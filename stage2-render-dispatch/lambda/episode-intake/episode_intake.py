# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import hashlib
import json
import logging
import os
import re
import urllib.parse
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

FEED_TABLE = os.environ["FEED_TABLE"]

# Only objects under this prefix are episodes that drive the render pipeline.
EPISODE_PREFIX = os.environ.get("EPISODE_PREFIX", "episodes/")

# State machine to start for each episode (empty during initial bootstrap).
STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")

_table = boto3.resource("dynamodb").Table(FEED_TABLE)
_sfn = boto3.client("stepfunctions")


def _variant_suffix(variant: int, variants: int) -> str:
    """Id suffix distinguishing an episode's variants.

    Empty for a single-render episode, so its execution name, feed sk and output
    prefix are exactly what they were before variants existed.
    """
    return "" if variants <= 1 else f"-v{variant}"


def _start_pipeline(episode_id: str, sk: str, bucket: str, key: str,
                    max_frames: int, variant: int = 0, variants: int = 1):
    """Start the render pipeline for one variant of an episode.

    The execution name = episodeId (+ '-v<i>' when fanning out) makes duplicate
    S3 events idempotent: a second event for the same object collides on the name
    and is rejected (ExecutionAlreadyExists), so no duplicate render. Including
    the variant keeps that guarantee per (object, variant) rather than per object,
    which is what lets one upload legitimately start N executions.
    """
    if not STATE_MACHINE_ARN:
        logger.warning("STATE_MACHINE_ARN not set; skipping pipeline start")
        return
    suffix = _variant_suffix(variant, variants)
    name = f"{episode_id}{suffix}"
    # Per-episode output prefix; the encode task writes one MP4 per camera under
    # it (e.g. output/<episodeId>/Cam_Context.mp4). One job renders both views.
    # Variants get their own subfolder so they don't overwrite each other.
    output_prefix = f"output/{episode_id}"
    if variants > 1:
        output_prefix += f"/v{variant}"
    payload = {
        "episodeId": episode_id,
        "sk": sk,
        "bucket": bucket,
        "key": key,
        "outputPrefix": output_prefix,
        # Frame cap for "test render" mode (0 = full episode). Always present so
        # the state machine can reference it as a container env override.
        "maxFrames": str(max_frames),
        # Drives per-variant randomization in the Blender task. Always present
        # for the same reason as maxFrames.
        "variant": str(variant),
    }
    try:
        _sfn.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=name,
            input=json.dumps(payload),
        )
        logger.info("Started pipeline execution %s", name)
    except _sfn.exceptions.ExecutionAlreadyExists:
        logger.info("Pipeline already running for %s (duplicate event)", name)


# Markers the presigner encodes into the key's folder segment, which is the only
# channel it has: the browser PUTs straight to S3 and intake sees just the key.
# Segment looks like "<ts>_<uuid>[-n<variants>][-test<frames>]"; the test marker
# stays last so its anchored pattern still matches.
_TEST_MARKER = re.compile(r"-test(\d+)$")
_VARIANTS_MARKER = re.compile(r"-n(\d+)")


def _max_frames_from_key(key: str) -> int:
    """Parse the optional `-test<N>` cap from the key's folder segment. Returns
    0 (no cap / full episode) when absent."""
    parts = key.split("/")
    if len(parts) < 2:
        return 0
    m = _TEST_MARKER.search(parts[-2])
    return int(m.group(1)) if m else 0


def _variants_from_key(key: str) -> int:
    """Parse the optional `-n<N>` variant count from the key's folder segment.
    Returns 1 (a single render) when absent."""
    parts = key.split("/")
    if len(parts) < 2:
        return 1
    m = _VARIANTS_MARKER.search(parts[-2])
    return max(1, int(m.group(1))) if m else 1


def _episode_id(key: str) -> str:
    """A stable id derived from the object key. Used as the DynamoDB record id
    AND the Step Functions execution name, so a duplicate S3 event for the same
    object is idempotent (same id => rejected duplicate execution)."""
    stem = os.path.splitext(os.path.basename(key))[0]
    # Execution names must be <=80 chars and [a-zA-Z0-9_-]. Sanitize + suffix a
    # short hash of the full key to avoid collisions across prefixes.
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in stem)[:60]
    digest = hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    return f"{safe}-{digest}"


def handler(event: dict, context) -> dict:
    """
    S3 ObjectCreated -> write one feed record per uploaded object.

    Everything needed (key, size, time) comes from the S3 event itself, so this
    function never reads from S3 and needs only DynamoDB write permission.

    For episode uploads (under EPISODE_PREFIX) the record is seeded with
    status=RECEIVED and a stable episodeId; the Step Functions pipeline (started
    in parallel via EventBridge) only advances the status from there.

    Feed record shape:
      pk = "FEED"                          (single partition for prototype scale)
      sk = "<uploadedAt ISO>#<episodeId>"  (descending query => newest first)
      episodeId, key, size, uploadedAt, status
    """
    records = event.get("Records", [])
    logger.info("Received %d S3 record(s)", len(records))

    for record in records:
        s3_info = record.get("s3", {})
        obj = s3_info.get("object", {})

        # S3 URL-encodes the key in the event (spaces -> '+', etc.). Decode it
        # so we store the real file name.
        raw_key = obj.get("key", "")
        key = urllib.parse.unquote_plus(raw_key)
        if not key:
            logger.warning("Skipping record with no object key: %s", record)
            continue

        # Only episode uploads belong in the feed / drive the pipeline. Asset
        # uploads (base scene, rig, textures under other prefixes) are ignored
        # so they don't clutter the UI.
        if not key.startswith(EPISODE_PREFIX):
            logger.info("Ignoring non-episode upload key=%s", key)
            continue

        size = int(obj.get("size", 0))
        uploaded_at = record.get("eventTime") or datetime.now(timezone.utc).isoformat()
        episode_id = _episode_id(key)
        max_frames = _max_frames_from_key(key)
        variants = _variants_from_key(key)
        bucket = s3_info.get("bucket", {}).get("name", "")

        # One upload can fan out into N independently randomized renders. Each
        # gets its own feed record (and so its own status ladder, progress and
        # delete), because every downstream writer updates a single sk.
        for variant in range(variants):
            suffix = _variant_suffix(variant, variants)
            item = {
                "pk": "FEED",
                "sk": f"{uploaded_at}#{episode_id}{suffix}",
                "episodeId": episode_id,
                "key": key,
                "size": size,
                "uploadedAt": uploaded_at,
                "status": "RECEIVED",
            }
            # Surface test renders in the feed so they're distinguishable in the UI.
            if max_frames > 0:
                item["testFrames"] = max_frames
            # Only tagged when fanning out, so single renders keep their old shape.
            if variants > 1:
                item["variant"] = variant
                item["variants"] = variants
            _table.put_item(Item=item)
            logger.info(
                "Recorded episode key=%s size=%d episode=%s maxFrames=%d "
                "variant=%d/%d", key, size, episode_id, max_frames,
                variant, variants,
            )
            _start_pipeline(episode_id, item["sk"], bucket, key, max_frames,
                            variant, variants)

    return {"statusCode": 200, "recorded": len(records)}

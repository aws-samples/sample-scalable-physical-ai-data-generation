# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

UPLOAD_BUCKET = os.environ["UPLOAD_BUCKET"]
EPISODE_PREFIX = os.environ.get("EPISODE_PREFIX", "episodes/")

# How long the presigned PUT URL stays valid (seconds).
URL_TTL_SECONDS = 300

# Most renders one upload may fan out into. Each variant is a full GPU render, so
# this is a cost guardrail as much as a validation bound.
MAX_VARIANTS = 10

# SigV4 for cross-region presigned URLs. `request_checksum_calculation` is set to
# "when_required" so boto3 does NOT bake a default CRC32 checksum header into the
# signed URL — browsers don't send that header, which otherwise causes
# SignatureDoesNotMatch on the PUT. (botocore >= 1.36 default is "when_supported".)
#
# The client targets the REGIONAL endpoint (<bucket>.s3.<region>.amazonaws.com).
# The default global endpoint answers a newly created bucket with a 307 redirect
# for up to ~24 h, and browsers refuse to follow a redirect on a cross-origin
# PUT ("Failed to fetch"). The upload bucket lives in the stack's region, which
# is the Lambda's AWS_REGION.
_REGION = os.environ["AWS_REGION"]
_s3 = boto3.client(
    "s3",
    region_name=_REGION,
    endpoint_url=f"https://s3.{_REGION}.amazonaws.com",
    config=Config(
        signature_version="s3v4",
        request_checksum_calculation="when_required",
        s3={"addressing_style": "virtual"},
    ),
)

# Accept only a safe file-name stem from the client; we build the final key.
_SAFE_STEM = re.compile(r"[^a-zA-Z0-9._-]+")


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handler(event: dict, context) -> dict:
    """
    POST /episodes (behind API Gateway + Cognito JWT authorizer).

    Body: { "filename": "my_episode.json" }  (optional)
    Returns a short-lived presigned PUT URL the browser uses to upload the
    episode JSON straight to S3 under the episodes/ prefix. The browser holds no
    S3 credentials; the upload then triggers the normal intake + pipeline.
    """
    try:
        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64

            raw = base64.b64decode(raw).decode("utf-8")
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, TypeError):
        return _response(400, {"error": "invalid_json_body"})

    requested = os.path.basename(str(payload.get("filename") or "episode.json"))
    safe_name = _SAFE_STEM.sub("-", requested)[:120]
    if not safe_name or safe_name == ".json":
        safe_name = "episode.json"
    if not safe_name.lower().endswith(".json"):
        safe_name += ".json"

    # Optional "test render" cap: render only the first N frames for quick
    # iteration. The browser PUTs the object straight to S3 and intake only sees
    # the key, so we carry the cap in the key's folder segment as a `-test<N>`
    # marker (intake parses it; the file's basename stays the user's filename).
    test_frames = 0
    try:
        test_frames = int(payload.get("testFrames") or 0)
    except (TypeError, ValueError):
        test_frames = 0
    test_frames = max(0, min(test_frames, 100000))  # clamp to a sane range
    marker = f"-test{test_frames}" if test_frames > 0 else ""

    # Optional variant count: render the same episode N times with independent
    # randomization. Carried in the key the same way as the frame cap, and placed
    # BEFORE it so the `-test<N>` marker stays at the end of the segment where
    # intake's anchored pattern expects it. Capped because each variant is a full
    # GPU render — the cost is linear in N.
    variants = 1
    try:
        variants = int(payload.get("variants") or 1)
    except (TypeError, ValueError):
        variants = 1
    variants = max(1, min(variants, MAX_VARIANTS))
    variants_marker = f"-n{variants}" if variants > 1 else ""

    # Uniqueness lives in a <timestamp>_<uuid> folder segment, so the object
    # keeps the user's original filename as its basename — the UI shows just
    # that name instead of a mangled key.
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    key = (f"{EPISODE_PREFIX}{ts}_{uuid.uuid4().hex[:8]}"
           f"{variants_marker}{marker}/{safe_name}")

    try:
        # Sign only Bucket + Key (no Content-Type): the browser can then send any
        # Content-Type without breaking the signature.
        url = _s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": UPLOAD_BUCKET, "Key": key},
            ExpiresIn=URL_TTL_SECONDS,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to presign upload URL")
        return _response(500, {"error": "internal_error"})

    logger.info("Issued presigned PUT for key=%s", key)
    return _response(200, {"uploadUrl": url, "key": key})

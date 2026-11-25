# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

FEED_TABLE = os.environ["FEED_TABLE"]
UPLOAD_BUCKET = os.environ["UPLOAD_BUCKET"]
OUTPUT_BUCKET = os.environ["OUTPUT_BUCKET"]

# Only terminal episodes may be deleted — deleting an in-progress record would
# be recreated by the still-running task (DynamoDB update_item upserts).
TERMINAL = {"DONE", "FAILED"}

_table = boto3.resource("dynamodb").Table(FEED_TABLE)
_s3 = boto3.client("s3")


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handler(event: dict, context) -> dict:
    """
    DELETE /episodes (behind API Gateway + Cognito JWT authorizer).

    Body: { "sk": "<record sk>", "key": "<episode s3 key>",
            "videoKey": "<output s3 key>" }

    Deletes the feed record and best-effort removes the episode JSON and the
    rendered video from S3. Refuses to delete a record that is still in flight.
    """
    try:
        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64

            raw = base64.b64decode(raw).decode("utf-8")
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, TypeError):
        return _response(400, {"error": "invalid_json_body"})

    sk = payload.get("sk")
    if not sk:
        return _response(400, {"error": "missing_sk"})

    # Guard: only delete terminal records.
    item = _table.get_item(Key={"pk": "FEED", "sk": sk}).get("Item")
    if item is not None:
        status = item.get("status")
        if status and status not in TERMINAL:
            return _response(
                409, {"error": "not_terminal", "status": status,
                      "message": "Cannot delete an episode while it is in progress."}
            )

    _table.delete_item(Key={"pk": "FEED", "sk": sk})

    # Best-effort S3 cleanup (missing objects are fine).
    key = payload.get("key")
    if key:
        try:
            _s3.delete_object(Bucket=UPLOAD_BUCKET, Key=key)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to delete upload object %s", key, exc_info=True)

    # Remove all rendered videos: the per-camera keys on the record (videoKeys),
    # plus the legacy single videoKey and any passed in the request body.
    video_keys: set[str] = set()
    if item:
        video_keys.update(v for v in (item.get("videoKeys") or {}).values() if v)
        if item.get("videoKey"):
            video_keys.add(item["videoKey"])
    for k in ("videoKey",):
        if payload.get(k):
            video_keys.add(payload[k])
    for vk in video_keys:
        try:
            _s3.delete_object(Bucket=OUTPUT_BUCKET, Key=vk)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to delete video object %s", vk, exc_info=True)

    logger.info("Deleted episode sk=%s (%d video object(s))", sk, len(video_keys))
    return _response(200, {"deleted": True})

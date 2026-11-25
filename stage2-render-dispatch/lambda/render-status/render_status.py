# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import decimal
import json
import logging
import os

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

FEED_TABLE = os.environ["FEED_TABLE"]
# Bucket holding rendered videos (presigned for in-browser playback).
OUTPUT_BUCKET = os.environ["OUTPUT_BUCKET"]

# Number of most-recent items returned to the web app.
FEED_LIMIT = 50

# How long each presigned playback/download URL stays valid (seconds).
URL_TTL_SECONDS = 3600

_table = boto3.resource("dynamodb").Table(FEED_TABLE)
# Signature v4 so presigned URLs are valid in all regions.
_s3 = boto3.client("s3", config=boto3.session.Config(signature_version="s3v4"))


def _presigned_url(bucket: str, key: str) -> str:
    """Short-lived signed GET URL so the browser can stream/download the object
    without the bucket being public."""
    return _s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=URL_TTL_SECONDS,
    )


def _to_jsonable(value):
    """DynamoDB returns numbers as Decimal; make them JSON-serializable."""
    if isinstance(value, decimal.Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    return value


def handler(event: dict, context) -> dict:
    """
    GET /feed (behind API Gateway + Cognito JWT authorizer).

    Returns the most recent episodes and their pipeline status as JSON:
      { "items": [ { "episodeId", "key", "status", "jobId", "progress",
                     "uploadedAt", "size", "videoUrl" }, ... ] }

    ScanIndexForward=False on the single "FEED" partition gives newest-first,
    since the sort key is prefixed with the ISO upload time.
    """
    try:
        resp = _table.query(
            KeyConditionExpression=Key("pk").eq("FEED"),
            ScanIndexForward=False,
            Limit=FEED_LIMIT,
        )
        items = []
        for it in resp.get("Items", []):
            # Only surface episode records (they carry a pipeline status). Skips
            # any legacy asset-upload records that predate the intake filter.
            if not it.get("status"):
                continue
            video_key = it.get("videoKey")
            # Per-camera videos (Cam -> S3 key), written by the encode task.
            # Presign each so the browser can play both views; keep the single
            # videoUrl (primary/context view) for the existing player.
            video_keys = it.get("videoKeys") or {}
            video_urls = {
                cam: _presigned_url(OUTPUT_BUCKET, key)
                for cam, key in video_keys.items()
                if key
            }
            items.append(
                {
                    "episodeId": it.get("episodeId"),
                    "sk": it.get("sk"),
                    "key": it.get("key"),
                    "videoKey": video_key,
                    "videoKeys": video_keys or None,
                    "size": _to_jsonable(it.get("size", 0)),
                    "uploadedAt": it.get("uploadedAt"),
                    "submittedAt": it.get("submittedAt"),
                    "status": it.get("status"),
                    "jobId": it.get("jobId"),
                    "progress": _to_jsonable(it.get("progress")),
                    "error": it.get("error"),
                    # Present (frame cap) only for "test render" episodes.
                    "testFrames": _to_jsonable(it.get("testFrames")),
                    # Present only when an upload fanned out into N renders, so
                    # the UI can label which variant a row is.
                    "variant": _to_jsonable(it.get("variant")),
                    "variants": _to_jsonable(it.get("variants")),
                    # Presigned URL for in-browser playback, only once rendered.
                    "videoUrl": _presigned_url(OUTPUT_BUCKET, video_key)
                    if video_key
                    else None,
                    "videoUrls": video_urls or None,
                }
            )
        body = json.dumps({"items": items})
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": body,
        }
    except Exception:  # noqa: BLE001 - top-level safety net
        logger.exception("Failed to list render status feed")
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "internal_error"}),
        }

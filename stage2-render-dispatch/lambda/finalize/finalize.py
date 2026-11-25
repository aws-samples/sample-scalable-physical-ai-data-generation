# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

FEED_TABLE = os.environ["FEED_TABLE"]

_table = boto3.resource("dynamodb").Table(FEED_TABLE)


def handler(event: dict, context) -> dict:
    """
    Terminal step of the pipeline. Input:
      success: { "episodeId", "sk", "videoKey" }
      failure: { "episodeId", "sk", "error" }

    Sets status=DONE (+ videoKey) or status=FAILED (+ error). This is the single
    place the record reaches a terminal state, satisfying the "terminal state
    always reached" correctness property.
    """
    sk = event["sk"]
    error = event.get("error")

    if error:
        # Only fall back to the (verbose) ECS/Step Functions cause if the failing
        # container didn't already record a concise reason. if_not_exists keeps
        # the container's message when present.
        _table.update_item(
            Key={"pk": "FEED", "sk": sk},
            UpdateExpression="SET #s = :s, #e = if_not_exists(#e, :e)",
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues={":s": "FAILED", ":e": str(error)[:1024]},
        )
        logger.info("Episode %s FAILED: %s", event.get("episodeId"), error)
        return {"status": "FAILED"}

    # The encode task records the per-camera videoKeys (+ primary videoKey) on
    # the record, so DONE just flips the status. A videoKey in the event is
    # still honored (legacy single-video path) but not required.
    video_key = event.get("videoKey")
    if video_key:
        _table.update_item(
            Key={"pk": "FEED", "sk": sk},
            UpdateExpression="SET #s = :s, videoKey = if_not_exists(videoKey, :v)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "DONE", ":v": video_key},
        )
    else:
        _table.update_item(
            Key={"pk": "FEED", "sk": sk},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "DONE"},
        )
    logger.info("Episode %s DONE", event.get("episodeId"))
    return {"status": "DONE"}

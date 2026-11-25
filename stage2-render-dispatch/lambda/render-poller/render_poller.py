# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

FEED_TABLE = os.environ["FEED_TABLE"]
FARM_ID_PARAM = os.environ["FARM_ID_PARAM"]
QUEUE_ID_PARAM = os.environ["QUEUE_ID_PARAM"]

# DynamoDB + SSM live in the stack region (Lambda's own region); Deadline Cloud
# may be in a different region (the farm's region).
DEADLINE_REGION = os.environ.get("DEADLINE_REGION") or os.environ.get("AWS_REGION")

_table = boto3.resource("dynamodb").Table(FEED_TABLE)
_ssm = boto3.client("ssm")
_deadline = boto3.client("deadline", region_name=DEADLINE_REGION)

# Deadline task-run states we treat as terminal.
_SUCCEEDED = {"SUCCEEDED"}
_FAILED = {"FAILED", "CANCELED", "NOT_COMPATIBLE"}


def _param(name: str) -> str:
    return _ssm.get_parameter(Name=name)["Parameter"]["Value"]


def _update_status(episode_id: str, sk: str, status: str, progress: dict | None):
    expr = "SET #s = :s"
    names = {"#s": "status"}
    values = {":s": status}
    if progress is not None:
        expr += ", progress = :p"
        values[":p"] = progress
    _table.update_item(
        Key={"pk": "FEED", "sk": sk},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def handler(event: dict, context) -> dict:
    """
    Invoked from the Step Functions wait loop. Input:
      { "episodeId", "sk" }
    The Deadline jobId is read from the DynamoDB record (written by the submit
    task), so it need not be threaded through the state machine.
    Returns:
      { "renderState": "RENDERING" | "COMPLETED" | "FAILED",
        "progress": { "done", "total" } }
    and patches the DynamoDB record so the UI shows live progress.
    """
    episode_id = event["episodeId"]
    sk = event["sk"]

    record = _table.get_item(Key={"pk": "FEED", "sk": sk}).get("Item", {})
    job_id = record.get("jobId")
    if not job_id:
        # Submit hasn't recorded a job yet; treat as still working.
        return {"renderState": "RENDERING", "progress": {"done": 0, "total": 0}}

    farm_id = _param(FARM_ID_PARAM)
    queue_id = _param(QUEUE_ID_PARAM)

    job = _deadline.get_job(farmId=farm_id, queueId=queue_id, jobId=job_id)
    lifecycle = job.get("lifecycleStatus", "")
    task_status = job.get("taskRunStatus", "")
    counts = job.get("taskRunStatusCounts", {}) or {}
    total = sum(counts.values()) if counts else 0
    done = counts.get("SUCCEEDED", 0)
    progress = {"done": int(done), "total": int(total)}

    logger.info(
        "job=%s lifecycle=%s taskRunStatus=%s counts=%s",
        job_id, lifecycle, task_status, counts,
    )

    if lifecycle.endswith("FAILED") or task_status in _FAILED:
        render_state = "FAILED"
    elif task_status in _SUCCEEDED:
        render_state = "COMPLETED"
    else:
        render_state = "RENDERING"

    if render_state == "RENDERING":
        _update_status(episode_id, sk, "RENDERING", progress)

    return {"renderState": render_state, "progress": progress}

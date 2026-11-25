#!/usr/bin/env python3
"""Video-encode task entrypoint (runs in Fargate).

Pipeline step 3:

  1. Read the Deadline jobId from the DynamoDB record.
  2. Download the completed job's rendered frames from Deadline Cloud.
  3. Stitch the frames into an MP4 with ffmpeg -- ONE video PER CAMERA.
  4. Upload each MP4 to the output bucket and record the keys on the record.

The render job renders two views (eye-in-hand + external context) as a single
Deadline job whose task space is (camera x frame); the template namespaces each
camera's frames into its own subfolder (OutputDir/<Camera>/frame_####). So here
we encode one MP4 per camera and store them under a per-episode prefix:

    <OUTPUT_PREFIX>/<Camera>.mp4     e.g. output/<episodeId>/Cam_Context.mp4

Inputs are supplied as environment variables (ECS container overrides set by the
state machine).

Back-compat: if OUTPUT_PREFIX is unset but OUTPUT_KEY is set, falls back to the
legacy single-video behavior (all frames -> one MP4 at OUTPUT_KEY).
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import sys

import boto3

REGION = os.environ.get("AWS_REGION", "us-west-2")
# Deadline Cloud farm region — may differ from the stack/S3/DDB region.
DEADLINE_REGION = os.environ.get("DEADLINE_REGION") or REGION
OUTPUT_BUCKET = os.environ["OUTPUT_BUCKET"]
# Per-episode output prefix (e.g. "output/<episodeId>"); one MP4 per camera lands
# under it. Legacy single-video mode uses OUTPUT_KEY instead.
OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX")
OUTPUT_KEY = os.environ.get("OUTPUT_KEY")
# Cameras rendered by the job; each has its own frame subfolder. Order matters
# only for picking the primary (back-compat) video below.
CAMERAS = [c.strip() for c in os.environ.get(
    "CAMERAS", "Cam_EyeInHand,Cam_Context").split(",") if c.strip()]
# The single video surfaced as the legacy `videoKey` (main UI player). Prefer
# the external context view; fall back to the first camera that produced frames.
PRIMARY_CAMERA = os.environ.get("PRIMARY_CAMERA", "Cam_Context")
FEED_TABLE = os.environ["FEED_TABLE"]
SK = os.environ["SK"]
FARM_ID_PARAM = os.environ["FARM_ID_PARAM"]
QUEUE_ID_PARAM = os.environ["QUEUE_ID_PARAM"]
FPS = os.environ.get("FPS", "30")

# `deadline job download-output` restores frames to the OUTPUT path recorded in
# the job — submit_to_deadline.py's --output-dir default,
# /opt/app/out/deadline_render (path-mapped to the same absolute path here).
SEARCH_ROOTS = ["/opt/app/out", "/tmp/frames"]
FRAME_EXTS = ("png", "PNG", "jpg", "JPG", "jpeg", "exr", "EXR", "tif", "tiff")

_s3 = boto3.client("s3", region_name=REGION)
_ssm = boto3.client("ssm", region_name=REGION)
_table = boto3.resource("dynamodb", region_name=REGION).Table(FEED_TABLE)


def set_status(status: str):
    _table.update_item(
        Key={"pk": "FEED", "sk": SK},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status},
    )


def set_video_keys(video_keys: dict, primary: str | None):
    """Record the per-camera video keys (map) plus a primary `videoKey` for the
    existing single-player UI. Best-effort; encode success is what matters."""
    expr = "SET videoKeys = :vk"
    values = {":vk": video_keys}
    if primary:
        expr += ", videoKey = :v"
        values[":v"] = primary
    _table.update_item(
        Key={"pk": "FEED", "sk": SK},
        UpdateExpression=expr,
        ExpressionAttributeValues=values,
    )


def set_failed(msg: str):
    """Record a concise failure reason so the UI shows something meaningful
    instead of the raw ECS task description."""
    try:
        _table.update_item(
            Key={"pk": "FEED", "sk": SK},
            UpdateExpression="SET #s = :s, #e = :e",
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues={":s": "FAILED", ":e": f"encode: {msg}"[:1000]},
        )
    except Exception:  # noqa: BLE001 - best-effort
        pass


def _log_cmd(argv: list[str]) -> None:
    print("+ " + " ".join(argv), flush=True)


def find_frames() -> list[str]:
    """All rendered frames under the search roots (both cameras' subfolders)."""
    frames: list[str] = []
    roots = SEARCH_ROOTS + [os.getcwd()]
    for root in roots:
        for ext in FRAME_EXTS:
            frames += glob.glob(os.path.join(root, "**", f"*.{ext}"), recursive=True)
    return sorted(set(frames))


def frames_for_camera(all_frames: list[str], camera: str) -> list[str]:
    """Frames belonging to `camera`, identified by the camera name appearing as
    a delimited token anywhere in the path.

    The render template asks the Blender adaptor to namespace each camera's
    frames into its own subfolder (OutputDir/<Camera>/frame_####), but the
    adaptor may instead flatten that into the FILENAME, e.g.
    `ViewLayer_Cam_Context_frame_0001.png`, dropping the subdirectory. To be
    robust to both layouts we match the camera name bounded by a path separator
    or an underscore/dot -- so `Cam_Context` matches both `/Cam_Context/frame`
    and `..._Cam_Context_frame...`, but never `Cam_ContextExtra` and never the
    other camera (`Cam_EyeInHand`)."""
    pat = re.compile(r"(?:^|[/_])" + re.escape(camera) + r"(?:[/_.]|$)")
    return sorted(f for f in all_frames if pat.search(f))


def encode_frames(frames: list[str], out_mp4: str) -> None:
    """Symlink frames into a zero-padded sequence and stitch them into out_mp4."""
    seq_dir = f"/tmp/seq_{os.path.splitext(os.path.basename(out_mp4))[0]}"
    os.makedirs(seq_dir, exist_ok=True)
    ext = os.path.splitext(frames[0])[1]
    for i, f in enumerate(frames):
        link = os.path.join(seq_dir, f"frame_{i:05d}{ext}")
        if not os.path.exists(link):
            os.symlink(os.path.abspath(f), link)
    ffmpeg_args = [
        "-y", "-framerate", FPS,
        "-i", os.path.join(seq_dir, f"frame_%05d{ext}"),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", out_mp4,
    ]
    _log_cmd(["ffmpeg", *ffmpeg_args])
    r = subprocess.run(["ffmpeg", *ffmpeg_args], capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0 or not os.path.isfile(out_mp4):
        raise RuntimeError("ffmpeg encode failed")


def upload(local_mp4: str, key: str) -> None:
    _s3.upload_file(
        local_mp4, OUTPUT_BUCKET, key,
        ExtraArgs={"ContentType": "video/mp4"},
    )
    print(f"uploaded s3://{OUTPUT_BUCKET}/{key}", flush=True)


def download_outputs(job_id: str) -> None:
    farm_id = _ssm.get_parameter(Name=FARM_ID_PARAM)["Parameter"]["Value"]
    queue_id = _ssm.get_parameter(Name=QUEUE_ID_PARAM)["Parameter"]["Value"]
    # The deadline CLI reads its region from the AWS region env; point it at the
    # farm's region (may differ from the stack region).
    deadline_env = dict(os.environ)
    deadline_env["AWS_REGION"] = DEADLINE_REGION
    deadline_env["AWS_DEFAULT_REGION"] = DEADLINE_REGION
    download_args = [
        "job", "download-output",
        "--farm-id", farm_id, "--queue-id", queue_id, "--job-id", job_id,
        "--output", "verbose", "--yes",
    ]
    _log_cmd(["deadline", *download_args])
    r = subprocess.run(
        ["deadline", *download_args],
        env=deadline_env, capture_output=True, text=True,
    )
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)


def main() -> int:
    set_status("ENCODING")

    record = _table.get_item(Key={"pk": "FEED", "sk": SK}).get("Item", {})
    job_id = record.get("jobId")
    if not job_id:
        raise RuntimeError("no jobId on record; cannot download outputs")

    download_outputs(job_id)

    all_frames = find_frames()
    if not all_frames:
        raise RuntimeError(f"no rendered frames found to encode under {SEARCH_ROOTS}")
    print(f"found {len(all_frames)} frames; first={all_frames[0]}", flush=True)

    # ---- Legacy single-video mode -----------------------------------------
    if not OUTPUT_PREFIX:
        if not OUTPUT_KEY:
            raise RuntimeError("neither OUTPUT_PREFIX nor OUTPUT_KEY is set")
        out_mp4 = "/tmp/output.mp4"
        encode_frames(all_frames, out_mp4)
        upload(out_mp4, OUTPUT_KEY)
        set_video_keys({"default": OUTPUT_KEY}, OUTPUT_KEY)
        return 0

    # ---- Per-camera mode ---------------------------------------------------
    prefix = OUTPUT_PREFIX.rstrip("/")
    produced: dict[str, str] = {}
    for cam in CAMERAS:
        cam_frames = frames_for_camera(all_frames, cam)
        if not cam_frames:
            print(f"[encode] no frames found for camera {cam!r}; skipping", flush=True)
            continue
        out_mp4 = f"/tmp/{cam}.mp4"
        print(f"[encode] {cam}: {len(cam_frames)} frames -> {out_mp4}", flush=True)
        encode_frames(cam_frames, out_mp4)
        key = f"{prefix}/{cam}.mp4"
        upload(out_mp4, key)
        produced[cam] = key

    # Fallback: the render didn't namespace frames per camera (e.g. the adaptor
    # ignored the per-task output subfolder). Encode everything into one video so
    # the episode still yields a result rather than failing outright.
    if not produced:
        cam = PRIMARY_CAMERA or (CAMERAS[0] if CAMERAS else "view")
        print(f"[encode] WARNING: no per-camera subfolders; encoding all frames "
              f"into a single {cam!r} video", flush=True)
        out_mp4 = f"/tmp/{cam}.mp4"
        encode_frames(all_frames, out_mp4)
        key = f"{prefix}/{cam}.mp4"
        upload(out_mp4, key)
        produced[cam] = key

    primary = produced.get(PRIMARY_CAMERA) or next(iter(produced.values()))
    set_video_keys(produced, primary)
    print(f"[encode] produced {len(produced)} video(s): {list(produced)}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        set_failed(str(exc))
        sys.exit(1)

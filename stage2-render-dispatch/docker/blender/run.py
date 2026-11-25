#!/usr/bin/env python3
"""Blender reconstruction + submit task entrypoint (runs in Fargate).

Pipeline step 1:

  1. Pull the base scene (scene_twin.blend), rig (rig.json) and the episode JSON
     from S3 into the layout the blender_twin/ scripts expect.
  2. bake_animation.py  -> ep.blend      (episode animation, no geometry rebuild)
  3. prepare_scene.py   -> ep_prepared.blend  (bake render/quality settings)
  4. submit_to_deadline.py --skip-prepare  -> submit to the existing farm/queue
  5. Record the Deadline jobId + status on the DynamoDB feed record.

All inputs are supplied as environment variables (set as ECS container overrides
by the Step Functions state machine).
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import boto3

APP_DIR = os.environ.get("APP_DIR", "/opt/app")
OUT_DIR = os.path.join(APP_DIR, "out")
WORK_DIR = "/tmp/work"

REGION = os.environ.get("AWS_REGION", "us-west-2")
# Deadline Cloud farm region — may differ from the stack/S3/DDB region.
DEADLINE_REGION = os.environ.get("DEADLINE_REGION") or REGION
UPLOAD_BUCKET = os.environ["UPLOAD_BUCKET"]
EPISODE_KEY = os.environ["EPISODE_KEY"]
BASE_SCENE_KEY = os.environ.get("BASE_SCENE_KEY", "base-scenes/scene_twin.blend")
RIG_KEY = os.environ.get("RIG_KEY", "base-scenes/rig.json")
# S3 prefix holding the texture zips (table_texture/ and box_texture/ subdirs).
# If empty / no objects present, texture randomization is skipped.
TEXTURE_PREFIX = os.environ.get("TEXTURE_PREFIX", "base-scenes/blender_texture/")
# S3 prefix holding distractor-object zips (Poly Haven <name>.blend.zip). If
# empty / no objects present, distractor placement is skipped.
DISTRACTOR_PREFIX = os.environ.get("DISTRACTOR_PREFIX", "base-scenes/distractor/")
# Per-episode HDRI environment (domain randomization): replace the sealed lab
# room with a random .exr/.hdr from BACKGROUND_PREFIX, which then both backs
# and LIGHTS the scene (image-based lighting). Mutually exclusive with the room
# by construction — the shell is removed for the episode, because an
# environment is invisible from inside a closed box. Set to 0/false/off to
# disable; skipped automatically when no HDRIs are present in S3.
BACKGROUND = os.environ.get("BACKGROUND", "1")
# Points at ONE HDRI set (a subfolder of base-scenes/background/), because the
# environment should suit the task being demonstrated. Must contain the .exr/.hdr
# files directly — randomize_background.py globs a single level, and the mirror
# below strips this prefix, so a nested folder would yield no matches.
BACKGROUND_PREFIX = os.environ.get(
    "BACKGROUND_PREFIX", "base-scenes/background/hospital/")
BACKGROUND_STRENGTH = os.environ.get("BACKGROUND_STRENGTH", "1.0")
# Per-episode random yaw of the drop-off bin (cube_open) for domain
# randomization. Constrained so the box still fits the rotated bin. Set to
# 0/false/off to disable. Uses the bin geometry already in the base scene, so no
# S3 asset is needed.
BIN_ROTATE = os.environ.get("BIN_ROTATE", "1")
# Per-episode external-camera pose jitter (domain randomization). Applies a
# small, seeded jitter to the over-the-shoulder context camera so each episode
# is observed from a slightly different viewpoint; the wrist camera is left
# rigid. Best-effort: skipped if the context camera isn't in the scene.
CAM_JITTER = os.environ.get("CAM_JITTER", "1")
CONTEXT_CAMERA = os.environ.get("CONTEXT_CAMERA", "Cam_Context")
CAM_POS_JITTER = os.environ.get("CAM_POS_JITTER", "0.08")
CAM_LOOKAT_JITTER = os.environ.get("CAM_LOOKAT_JITTER", "0.05")
# Cameras rendered per episode. Submitted as a single Deadline job whose task
# space expands over (camera x frame), so one job produces both the eye-in-hand
# and the external context views. Comma-separated; order maps to CameraA,CameraB.
CAMERAS = os.environ.get("CAMERAS", "Cam_EyeInHand,Cam_Context")
# "Test render" frame cap (0 = full episode). When > 0, only the first
# MAX_FRAMES frames are submitted, for quick iteration on look/setup.
MAX_FRAMES = os.environ.get("MAX_FRAMES", "0")
FEED_TABLE = os.environ["FEED_TABLE"]
SK = os.environ["SK"]
EPISODE_ID = os.environ["EPISODE_ID"]
# Which render this is among the episode's variants ("0" for a single render).
# One episode can be rendered N times with independent randomization; the index
# is what makes each variant differ, since every randomizer is seeded off the
# episode id and would otherwise reproduce the same scene N times.
VARIANT = os.environ.get("VARIANT", "0")
try:
    VARIANT_INDEX = max(0, int(VARIANT))
except (TypeError, ValueError):
    VARIANT_INDEX = 0
# Seed for the per-episode randomizers. Variant 0 keeps the bare episode id, so
# single-render episodes reproduce exactly as they did before variants existed.
EPISODE_SEED = EPISODE_ID if VARIANT_INDEX == 0 else f"{EPISODE_ID}-v{VARIANT_INDEX}"
FARM_ID_PARAM = os.environ["FARM_ID_PARAM"]
QUEUE_ID_PARAM = os.environ["QUEUE_ID_PARAM"]

# Render/quality settings baked into the prepared scene. Sensible defaults that
# match submit_to_deadline.py; overridable via env.
SETTINGS = {
    "engine": os.environ.get("RENDER_ENGINE", "CYCLES"),
    "res_percent": int(os.environ.get("RES_PERCENT", "50")),
    "samples": int(os.environ.get("SAMPLES", "256")),
    "noise_threshold": float(os.environ.get("NOISE_THRESHOLD", "0.01")),
    "denoise": os.environ.get("DENOISE", "ON"),
    "exposure": float(os.environ.get("EXPOSURE", "1.239")),
    "gamma": float(os.environ.get("GAMMA", "0.552")),
    "view_transform": os.environ.get("VIEW_TRANSFORM", "Filmic"),
    "look": os.environ.get("LOOK", "High Contrast"),
}

_s3 = boto3.client("s3", region_name=REGION)
_ssm = boto3.client("ssm", region_name=REGION)
_table = boto3.resource("dynamodb", region_name=REGION).Table(FEED_TABLE)


def set_status(status: str, extra: dict | None = None):
    expr = "SET #s = :s"
    names = {"#s": "status"}
    values = {":s": status}
    for i, (k, v) in enumerate((extra or {}).items()):
        expr += f", {k} = :v{i}"
        values[f":v{i}"] = v
    _table.update_item(
        Key={"pk": "FEED", "sk": SK},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
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
            ExpressionAttributeValues={":s": "FAILED", ":e": f"reconstruct/submit: {msg}"[:1000]},
        )
    except Exception:  # noqa: BLE001 - best-effort
        pass


def _log_cmd(argv: list[str]) -> None:
    print("+ " + " ".join(argv), flush=True)


def run_blender(*args: str) -> subprocess.CompletedProcess:
    """Run headless Blender with the given arguments (argv list, no shell)."""
    _log_cmd(["blender", "--factory-startup", "--background", *args])
    return subprocess.run(
        ["blender", "--factory-startup", "--background", *args],
        capture_output=True, text=True,
    )


def download_prefix(bucket: str, prefix: str, dest: str) -> int:
    """Download every object under `prefix` into `dest`, preserving the relative
    structure. Returns the number of files downloaded."""
    paginator = _s3.get_paginator("list_objects_v2")
    count = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            rel = key[len(prefix):].lstrip("/")
            local = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(local), exist_ok=True)
            _s3.download_file(bucket, key, local)
            count += 1
    return count


def maybe_randomize_background(base_scene: str) -> str | None:
    """Optional domain-randomization step. If HDRIs exist under
    BACKGROUND_PREFIX and BACKGROUND is enabled, remove the sealed room shell
    and light/back the scene with a random environment at a random yaw (see
    randomize_background.py), returning the path to the new scene, or None if
    disabled / no assets / failure.

    Seeded by EPISODE_ID for reproducibility. Best-effort: any failure logs and
    falls back to the room-lit scene rather than failing the episode.
    """
    if BACKGROUND.strip().lower() in ("0", "false", "no", "off", ""):
        print("[background] disabled via BACKGROUND; skipping", flush=True)
        return None
    if not BACKGROUND_PREFIX:
        return None
    bg_root = "/tmp/background"
    n = download_prefix(UPLOAD_BUCKET, BACKGROUND_PREFIX, bg_root)
    if n == 0:
        print(f"[background] no HDRIs under {BACKGROUND_PREFIX}; skipping",
              flush=True)
        return None

    out = os.path.join(WORK_DIR, "scene_background.blend")
    # --python-exit-code makes Blender return non-zero if the script raises, so
    # we can fall back cleanly to the room-lit scene.
    r = run_blender(
        base_scene,
        "--python-exit-code", "1",
        "--python", os.path.join(APP_DIR, "randomize_background.py"), "--",
        "--background-dir", bg_root,
        # Deliberately the BARE episode id, not EPISODE_SEED: the HDRI choice is
        # stratified, so every variant must derive the same starting offset and
        # differ only by --variant. Salting the seed per variant would break the
        # guarantee that siblings get distinct environments.
        "--seed", EPISODE_ID,
        "--variant", str(VARIANT_INDEX),
        "--strength", BACKGROUND_STRENGTH,
        "--output", out,
    )
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0 or not os.path.isfile(out):
        tail = (r.stderr or r.stdout or "").strip()[-600:]
        print(f"[background] skipped (non-fatal): {tail}", flush=True)
        return None

    print(f"[background] using scene with HDRI environment: {out}", flush=True)
    return out


def maybe_randomize_textures(base_scene: str) -> str | None:
    """Optional domain-randomization step. If texture zips exist under
    TEXTURE_PREFIX in S3, download them and run randomize_textures.py on the base
    scene, returning the path to the randomized .blend. Otherwise return None
    (pipeline proceeds with the plain base scene).

    randomize_textures.py resolves its output relative to its own directory and
    saves to <script_dir>/blender_scene/scene_twin_table-<X>_box-<Y>.blend, so we
    create that dir and glob for the produced file afterwards.
    """
    if not TEXTURE_PREFIX:
        return None
    tex_root = "/tmp/blender_texture"
    n = download_prefix(UPLOAD_BUCKET, TEXTURE_PREFIX, tex_root)
    if n == 0:
        print(f"[randomize] no textures under {TEXTURE_PREFIX}; skipping", flush=True)
        return None

    table_dir = os.path.join(tex_root, "table_texture")
    box_dir = os.path.join(tex_root, "box_texture")
    scene_out_dir = os.path.join(APP_DIR, "blender_scene")
    os.makedirs(scene_out_dir, exist_ok=True)

    # --python-exit-code makes Blender return non-zero if the script raises
    # (by default Blender swallows script exceptions and still exits 0).
    r = run_blender(
        base_scene,
        "--python-exit-code", "1",
        "--python", os.path.join(APP_DIR, "randomize_textures.py"), "--",
        "--table-dir", table_dir, "--box-dir", box_dir,
    )
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip()[-600:]
        raise RuntimeError(f"randomize_textures failed: {tail}")

    produced = sorted(
        glob.glob(os.path.join(scene_out_dir, "scene_twin_*.blend")),
        key=os.path.getmtime,
    )
    if not produced:
        tail = (r.stdout or "").strip()[-600:]
        raise RuntimeError(
            f"randomize_textures produced no output in {scene_out_dir}. "
            f"Blender output tail: {tail}"
        )
    randomized = produced[-1]
    print(f"[randomize] using randomized scene: {randomized}", flush=True)
    return randomized


def maybe_place_distractor(base_scene: str, episode_path: str) -> str | None:
    """Optional domain-randomization step. If distractor zips exist under
    DISTRACTOR_PREFIX in S3, download them and run place_distractor.py on the
    (possibly texture-randomized) base scene, returning the path to the scene
    with a distractor placed on the table. Otherwise return None.

    Placement is seeded by EPISODE_ID so a given episode always gets the same
    distractor at the same spot (reproducible datasets). The distractor is
    parented to the table, so it rides the per-episode table-height shift that
    bake_animation.py applies next.

    Best-effort: distractor placement is a nicety, not required for a valid
    render, so any failure (no valid spot, bad asset, script error) logs and
    falls back to the input scene rather than failing the episode.
    """
    if not DISTRACTOR_PREFIX:
        return None
    dist_root = "/tmp/distractor"
    n = download_prefix(UPLOAD_BUCKET, DISTRACTOR_PREFIX, dist_root)
    if n == 0:
        print(f"[distractor] no assets under {DISTRACTOR_PREFIX}; skipping", flush=True)
        return None

    out = os.path.join(WORK_DIR, "scene_distractor.blend")
    # --python-exit-code makes Blender return non-zero if the script raises, so
    # we can fall back cleanly to the input scene.
    r = run_blender(
        base_scene,
        "--python-exit-code", "1",
        "--python", os.path.join(APP_DIR, "place_distractor.py"), "--",
        "--episode", episode_path,
        "--distractor-dir", dist_root,
        "--output", out,
        "--seed", EPISODE_SEED,
    )
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0 or not os.path.isfile(out):
        tail = (r.stderr or r.stdout or "").strip()[-600:]
        print(f"[distractor] skipped (non-fatal): {tail}", flush=True)
        return None

    print(f"[distractor] using scene with distractor: {out}", flush=True)
    return out


def maybe_rotate_bin(base_scene: str, episode_path: str) -> str | None:
    """Optional domain-randomization step. Rotate the drop-off bin (cube_open)
    about its vertical axis by a random yaw, returning the path to the rotated
    scene, or None if the step is disabled or fails.

    Seeded by EPISODE_ID for reproducibility. The rotation is constrained so the
    box's recorded final placement still fits the rotated bin no worse than in
    the original recording (see rotate_bin.py). The bin is a table-group object,
    so it rides the per-episode table-height shift applied by bake_animation.py.

    Best-effort: bin rotation is a nicety, not required for a valid render, so
    any failure logs and falls back to the input scene rather than failing the
    episode.
    """
    if BIN_ROTATE.strip().lower() in ("0", "false", "no", "off"):
        print("[rotate-bin] disabled via BIN_ROTATE; skipping", flush=True)
        return None

    out = os.path.join(WORK_DIR, "scene_binrot.blend")
    # --python-exit-code makes Blender return non-zero if the script raises, so
    # we can fall back cleanly to the input scene.
    r = run_blender(
        base_scene,
        "--python-exit-code", "1",
        "--python", os.path.join(APP_DIR, "rotate_bin.py"), "--",
        "--episode", episode_path,
        "--output", out,
        "--seed", EPISODE_SEED,
        "--force",
    )
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0 or not os.path.isfile(out):
        tail = (r.stderr or r.stdout or "").strip()[-600:]
        print(f"[rotate-bin] skipped (non-fatal): {tail}", flush=True)
        return None

    print(f"[rotate-bin] using scene with rotated bin: {out}", flush=True)
    return out


def maybe_jitter_camera(prepared_scene: str) -> str:
    """Optional per-episode external-camera pose jitter (domain randomization).

    Applies a small, seeded jitter to the over-the-shoulder context camera on the
    prepared scene so each episode is observed from a slightly different
    viewpoint. Seeded by EPISODE_ID for reproducibility; the wrist (eye-in-hand)
    camera is intentionally left untouched by randomize_camera.py.

    Best-effort: if jitter is disabled, the context camera isn't present, or the
    step fails for any reason, log and fall back to the un-jittered scene so the
    render still proceeds.
    """
    if CAM_JITTER.strip().lower() in ("0", "false", "no", "off"):
        print("[cam-jitter] disabled via CAM_JITTER; skipping", flush=True)
        return prepared_scene

    jittered = os.path.join(WORK_DIR, "ep_jittered.blend")
    # --python-exit-code makes Blender return non-zero if the script raises
    # (e.g. the context camera isn't in the scene), so we can fall back cleanly.
    r = run_blender(
        prepared_scene,
        "--python-exit-code", "1",
        "--python", os.path.join(APP_DIR, "randomize_camera.py"), "--",
        "--camera", CONTEXT_CAMERA,
        "--seed", EPISODE_SEED,
        "--pos-jitter", CAM_POS_JITTER,
        "--lookat-jitter", CAM_LOOKAT_JITTER,
        "--output", jittered,
    )
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0 or not os.path.isfile(jittered):
        tail = (r.stderr or r.stdout or "").strip()[-600:]
        print(f"[cam-jitter] skipped (non-fatal): {tail}", flush=True)
        return prepared_scene

    print(f"[cam-jitter] using jittered scene: {jittered}", flush=True)
    return jittered


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(WORK_DIR, exist_ok=True)

    set_status("RECONSTRUCTING")

    # 1. Pull inputs.
    target_scene = os.path.join(OUT_DIR, "scene_twin.blend")
    rig_path = os.path.join(OUT_DIR, "rig.json")
    episode_path = os.path.join(WORK_DIR, "episode.json")
    _s3.download_file(UPLOAD_BUCKET, BASE_SCENE_KEY, target_scene)
    _s3.download_file(UPLOAD_BUCKET, RIG_KEY, rig_path)
    _s3.download_file(UPLOAD_BUCKET, EPISODE_KEY, episode_path)

    # 1a. Optionally replace the sealed room with a random HDRI environment
    #     (domain randomization): the environment then both backs and lights the
    #     scene. Runs before the lighting step so that step's world-strength
    #     jitter applies to the HDRI. Best-effort.
    with_bg = maybe_randomize_background(target_scene)
    if with_bg:
        target_scene = with_bg

    # 1b. Optionally randomize table/box textures on the base scene (domain
    #     randomization). If textures exist in S3, bake the episode onto the
    #     randomized scene instead of the plain one.
    randomized = maybe_randomize_textures(target_scene)
    if randomized:
        target_scene = randomized

    # 1c. Optionally place a random distractor object on the table (domain
    #     randomization), inside the context-camera FOV and clear of the cube's
    #     table-contact footprint. Seeded by EPISODE_ID; parented to the table so
    #     it rides the table-height shift applied during baking. Best-effort.
    with_distractor = maybe_place_distractor(target_scene, episode_path)
    if with_distractor:
        target_scene = with_distractor

    # 1d. Optionally rotate the drop-off bin (cube_open) by a random yaw (domain
    #     randomization), constrained so the box still fits the rotated bin.
    #     Seeded by EPISODE_ID; the bin is a table-group object so it rides the
    #     table-height shift during baking. Best-effort.
    with_binrot = maybe_rotate_bin(target_scene, episode_path)
    if with_binrot:
        target_scene = with_binrot

    # 2. Reconstruct: bake the episode onto the (possibly randomized) base scene.
    ep_blend = os.path.join(WORK_DIR, "ep.blend")
    r = run_blender(
        "--python", os.path.join(APP_DIR, "bake_animation.py"), "--",
        APP_DIR, episode_path, target_scene, ep_blend,
    )
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0 or not os.path.isfile(ep_blend):
        raise RuntimeError("bake_animation failed")

    # 3. Bake render/quality settings into a prepared copy.
    prepared = os.path.join(WORK_DIR, "ep_prepared.blend")
    r = run_blender(
        ep_blend,
        "--python", os.path.join(APP_DIR, "prepare_scene.py"), "--",
        json.dumps(SETTINGS), prepared,
    )
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0 or not os.path.isfile(prepared):
        raise RuntimeError("prepare_scene failed")

    # 3b. Optionally jitter the external context camera's pose (domain
    #     randomization). Best-effort; falls back to the un-jittered scene.
    prepared = maybe_jitter_camera(prepared)

    # 4. Submit to Deadline Cloud (existing farm/queue from SSM; auth = task role).
    farm_id = _ssm.get_parameter(Name=FARM_ID_PARAM)["Parameter"]["Value"]
    queue_id = _ssm.get_parameter(Name=QUEUE_ID_PARAM)["Parameter"]["Value"]
    # The deadline library reads its region from the AWS region env; point the
    # submit subprocess at the farm's region (may differ from the stack region).
    submit_env = dict(os.environ)
    submit_env["AWS_REGION"] = DEADLINE_REGION
    submit_env["AWS_DEFAULT_REGION"] = DEADLINE_REGION
    submit_args = [
        "--scene", prepared, "--skip-prepare",
        "--farm-id", farm_id, "--queue-id", queue_id,
        "--name", EPISODE_ID,
        # One job, both views: the template expands over (camera x frame).
        "--cameras", CAMERAS,
    ]
    try:
        max_frames = int(MAX_FRAMES)
    except (TypeError, ValueError):
        max_frames = 0
    if max_frames > 0:
        submit_args += ["--max-frames", str(max_frames)]
        print(f"[test-mode] capping render to the first {max_frames} frames", flush=True)
    # Run the submitter as a module from APP_DIR (argv list, no shell).
    _log_cmd(["python3", "-m", "submit_to_deadline", *submit_args])
    r = subprocess.run(
        ["python3", "-m", "submit_to_deadline", *submit_args],
        cwd=APP_DIR, env=submit_env, capture_output=True, text=True,
    )
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0:
        raise RuntimeError("submit_to_deadline failed")

    m = re.search(r"Submitted job:\s*(job-[0-9a-f]+)", r.stdout)
    if not m:
        raise RuntimeError("could not parse Deadline job id from submit output")
    job_id = m.group(1)

    # 5. Record the job id + submission time so the poller/encoder can find it
    #    and the UI can show when the render was submitted to Deadline.
    submitted_at = datetime.now(timezone.utc).isoformat()
    set_status("SUBMITTED", {"jobId": job_id, "submittedAt": submitted_at})
    print(f"submitted jobId={job_id} at {submitted_at}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        set_failed(str(exc))
        sys.exit(1)

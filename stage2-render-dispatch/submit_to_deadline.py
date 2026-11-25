#!/usr/bin/env python3
"""Submit the Blender twin scene to an existing AWS Deadline Cloud fleet/queue,
using the Blender ADAPTOR (sticky rendering).

This does NOT use the Blender submitter add-on. It uses the open-source
`deadline` Python library to push the job bundle in ./deadline_bundle, whose
template drives the `blender-openjd` adaptor so Blender stays open between
frames on each worker.

Because the adaptor renders the settings SAVED IN THE .blend (it doesn't run a
render-time override script), this tool first bakes your quality / color
settings into a prepared copy of the scene with prepare_scene.py (via a local
Blender), then submits that prepared copy. Use --skip-prepare to submit the
scene as-is.

Prerequisites
-------------
1. pip install deadline
2. AWS credentials for the configured profile.
3. A farm + queue that exist, with a Conda queue environment that installs
   `blender` AND `deadline-cloud-for-blender` on the worker.
4. A local Blender (auto-detected, or pass --blender) to bake the prepared scene.

Usage
-----
    python3 submit_to_deadline.py --scene out/scene_twin.blend --frames 1-378
    python3 submit_to_deadline.py --scene out/office.blend --skip-prepare
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BUNDLE = os.path.join(HERE, "deadline_bundle")
DEFAULT_CONFIG_FILE = os.path.join(HERE, "deadline_config")
PREPARE_SCRIPT = os.path.join(HERE, "prepare_scene.py")


def clamp_frames(frames: str, max_frames: int) -> str:
    """Keep only the first `max_frames` frames of a frame spec and return a
    compact spec (e.g. clamp('1-550', 100) -> '1-100'). Handles comma lists and
    ranges; falls back to the input if it can't be parsed. max_frames <= 0 is a
    no-op."""
    if max_frames <= 0:
        return frames
    nums: set[int] = set()
    for tok in str(frames).split(","):
        tok = tok.strip()
        if not tok:
            continue
        if "-" in tok:
            a, _, b = tok.partition("-")
            try:
                nums.update(range(int(a), int(b) + 1))
            except ValueError:
                continue
        else:
            try:
                nums.add(int(tok))
            except ValueError:
                continue
    if not nums:
        return frames
    kept = sorted(nums)[:max_frames]
    # Collapse consecutive frames back into compact ranges.
    parts: list[str] = []
    start = prev = kept[0]
    for n in kept[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(f"{start}-{prev}" if start != prev else f"{start}")
        start = prev = n
    parts.append(f"{start}-{prev}" if start != prev else f"{start}")
    return ",".join(parts)


def detect_frame_range(scene_path: str) -> str | None:
    """Read the frame range from the scene's render-settings sidecar
    ('<scene>.deadline_render_settings.json', key 'frame_list')."""
    base, _ext = os.path.splitext(scene_path)
    sidecar = base + ".deadline_render_settings.json"
    if not os.path.isfile(sidecar):
        return None
    try:
        with open(sidecar) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    frames = data.get("frame_list")
    return str(frames) if frames else None


def find_blender(explicit: str | None) -> str | None:
    """Locate a local Blender executable."""
    import shutil

    def _ok(path: str) -> str | None:
        # Absolute, existing, executable file only (never a bare/dash-prefixed arg).
        path = os.path.abspath(path)
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else None

    if explicit:
        return _ok(explicit)
    on_path = shutil.which("blender")
    if on_path:
        return _ok(on_path)
    return _ok("/Applications/Blender.app/Contents/MacOS/Blender")


def read_scene_frame_range(blender: str, scene: str) -> str | None:
    """Read frame_start-frame_end from the .blend via a headless Blender."""
    expr = (
        "import bpy;"
        "s=bpy.data.scenes[0];"
        "print('FRAME_RANGE=%d-%d' % (s.frame_start, s.frame_end))"
    )
    try:
        # argv list (no shell). argv[0] is a display name; the validated path
        # from find_blender() is what actually runs, via executable=.
        out = subprocess.run(
            ["blender", "-b", scene, "--python-expr", expr],
            executable=blender,
            capture_output=True, text=True, timeout=120,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"FRAME_RANGE=(\d+-\d+)", out)
    return m.group(1) if m else None


def read_scene_render_target(blender: str, scene: str) -> tuple[str | None, str | None]:
    """Read the active scene's name and its active camera's name from the .blend.

    The adaptor defaults to a scene called 'Scene' and a camera called 'Camera';
    if the .blend uses different names the render fails ('Camera ... does not
    exist'). Auto-detecting them here keeps the submit command name-agnostic.
    """
    expr = (
        "import bpy;"
        "s=bpy.context.scene;"
        "c=s.camera;"
        "print('RENDER_SCENE=%s' % s.name);"
        "print('RENDER_CAMERA=%s' % (c.name if c else ''))"
    )
    try:
        out = subprocess.run(
            ["blender", "-b", scene, "--python-expr", expr],
            executable=blender,
            capture_output=True, text=True, timeout=120,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    sm = re.search(r"RENDER_SCENE=(.+)", out)
    cm = re.search(r"RENDER_CAMERA=(.+)", out)
    scene_name = sm.group(1).strip() if sm else None
    camera_name = cm.group(1).strip() if cm else None
    return scene_name, (camera_name or None)


def bake_prepared_scene(blender: str, scene: str, settings: dict, out_path: str) -> bool:
    """Run prepare_scene.py in headless Blender to bake settings into out_path."""
    print(f"[prepare] baking settings into {out_path} ...")
    try:
        r = subprocess.run(
            ["blender", "-b", scene, "--python", PREPARE_SCRIPT, "--",
             json.dumps(settings), out_path],
            executable=blender,
            capture_output=True, text=True, timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as e:
        sys.stderr.write(f"prepare_scene failed to launch: {e}\n")
        return False
    if r.returncode != 0 or not os.path.isfile(out_path):
        sys.stderr.write("prepare_scene did not produce an output scene.\n")
        sys.stderr.write(r.stdout[-2000:] + "\n" + r.stderr[-2000:] + "\n")
        return False
    return True


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scene", default=os.path.join(HERE, "out", "scene_twin.blend"),
                   help="Path to the .blend to render (default: out/scene_twin.blend).")
    p.add_argument("--frames", default=None,
                   help='Frame range, e.g. "1-378". Default: auto-detected from '
                        'the .deadline_render_settings.json sidecar, else read '
                        'from the .blend timeline.')
    p.add_argument("--max-frames", type=int, default=0,
                   help="Cap the render to the first N frames (test render). "
                        "0 = no cap (full range). Applied after the frame range "
                        "is resolved.")
    p.add_argument("--blender", default=None,
                   help="Path to a local Blender (for baking the prepared scene "
                        "and reading the frame range). Auto-detected if omitted.")
    p.add_argument("--skip-prepare", action="store_true",
                   help="Submit the scene as-is without baking settings into a "
                        "prepared copy (use if the .blend already has them).")
    p.add_argument("--output-dir", default=os.path.join(HERE, "out", "deadline_render"),
                   help="Local directory rendered frames are downloaded to.")
    p.add_argument("--format", default="PNG",
                   choices=["PNG", "JPEG", "OPEN_EXR", "TIFF", "WEBP", "TGA"],
                   help="Output image format (default: PNG).")
    p.add_argument("--gpu-device", default="OPTIX",
                   choices=["OPTIX", "CUDA", "HIP", "ONEAPI", "METAL", "NONE"],
                   help="Adaptor GPU compute backend (default: OPTIX for NVIDIA).")
    p.add_argument("--conda-packages", default="blender=4.5.* blender-openjd=0.6.*",
                   help="Conda packages for the queue environment. Must include "
                        "the adaptor package `blender-openjd`.")
    p.add_argument("--conda-channels", default="deadline-cloud",
                   help="Conda channels for the queue environment (default: "
                        "deadline-cloud).")
    # Quality / color settings -> baked into the prepared scene by prepare_scene.py
    p.add_argument("--engine", default="CYCLES", choices=["CYCLES", "EEVEE"],
                   help="Render engine (default: CYCLES).")
    p.add_argument("--res-percent", type=int, default=50,
                   help="Resolution scale %% (default: 50 = half).")
    p.add_argument("--res-x", type=int, default=0,
                   help="Base output width in px. 0 = keep scene resolution.")
    p.add_argument("--res-y", type=int, default=0,
                   help="Base output height in px. 0 = keep scene resolution.")
    p.add_argument("--samples", type=int, default=256,
                   help="Max render samples (default: 256).")
    p.add_argument("--noise-threshold", type=float, default=0.01,
                   help="Cycles adaptive-sampling noise threshold (default 0.01; "
                        "0 disables adaptive sampling).")
    p.add_argument("--light-threshold", type=float, default=0.01,
                   help="Cycles light sampling threshold (default: 0.01).")
    p.add_argument("--denoise", default="ON", choices=["SCENE", "ON", "OFF"],
                   help="Cycles denoiser (default: ON = OpenImageDenoise).")
    p.add_argument("--exposure", type=float, default=1.239,
                   help="Color-management exposure in stops (default: 1.239).")
    p.add_argument("--gamma", type=float, default=0.552,
                   help="Color-management gamma (default: 0.552).")
    p.add_argument("--view-transform", default="Filmic",
                   help="View transform (default: Filmic).")
    p.add_argument("--look", default="High Contrast",
                   help="Contrast look (default: 'High Contrast').")
    p.add_argument("--camera", default=None,
                   help="Camera (by name) to render from. Default: auto-detected "
                        "active camera of the scene (read from the .blend).")
    p.add_argument("--cameras", default=None,
                   help="Comma-separated camera names to render as ONE job with a "
                        "(camera x frame) task space, e.g. "
                        "'Cam_EyeInHand,Cam_Context'. Overrides --camera. Default: "
                        "the single --camera / auto-detected active camera.")
    p.add_argument("--render-scene", default=None,
                   help="Scene (by name) to render. Default: auto-detected active "
                        "scene (read from the .blend).")
    p.add_argument("--name", default=None,
                   help="Job name (default: <scene>_<YYYYmmdd_HHMMSS>).")
    p.add_argument("--bundle", default=DEFAULT_BUNDLE,
                   help="Job bundle directory (default: ./deadline_bundle).")
    p.add_argument("--config-file", default=DEFAULT_CONFIG_FILE,
                   help="Deadline Cloud config INI (default: ./deadline_config).")
    p.add_argument("--farm-id", default=None, help="Override the default farm id.")
    p.add_argument("--queue-id", default=None, help="Override the default queue id.")
    p.add_argument("--profile", default=None, help="Override the AWS profile name.")
    p.add_argument("--max-workers", type=int, default=None,
                   help="Max concurrent workers for this job (default: fleet max).")
    p.add_argument("--priority", type=int, default=None,
                   help="Scheduling priority (higher runs first; default 50).")
    # Deprecated: chunking is obsolete with the sticky adaptor (one task per
    # frame). Accepted as no-ops so old command lines don't break.
    p.add_argument("--tasks-per-gpu", type=int, default=None,
                   help=argparse.SUPPRESS)
    p.add_argument("--frames-per-task", default=None, help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.tasks_per_gpu is not None or args.frames_per_task is not None:
        sys.stderr.write(
            "[note] --tasks-per-gpu / --frames-per-task are ignored with the "
            "adaptor: it keeps Blender open and renders one task per frame, so "
            "chunking is unnecessary.\n")
    return args


def main() -> int:
    args = parse_args()

    if args.config_file and os.path.isfile(args.config_file):
        os.environ.setdefault("DEADLINE_CONFIG_FILE_PATH",
                              os.path.abspath(args.config_file))

    try:
        from deadline.client import api
        from deadline.client.config import config_file
    except ImportError:
        sys.stderr.write("The 'deadline' library is not installed.\n"
                         "Install it with:  pip install deadline\n")
        return 1

    scene = os.path.abspath(args.scene)
    if not os.path.isfile(scene):
        sys.stderr.write(f"Scene file not found: {scene}\n")
        return 1
    if not os.path.isdir(args.bundle):
        sys.stderr.write(f"Job bundle directory not found: {args.bundle}\n")
        return 1

    blender = find_blender(args.blender)

    # Resolve the frame range: --frames > sidecar > read from .blend.
    frames = args.frames or detect_frame_range(scene)
    if not frames and blender:
        frames = read_scene_frame_range(blender, scene)
        if frames:
            print(f"[frames] read {frames} from scene timeline via Blender")
    if not frames:
        sys.stderr.write("Could not determine the frame range. Pass --frames.\n")
        return 1

    # Test render: cap to the first N frames after resolving the full range.
    if args.max_frames and args.max_frames > 0:
        capped = clamp_frames(frames, args.max_frames)
        if capped != frames:
            print(f"[frames] capping {frames} -> {capped} (--max-frames "
                  f"{args.max_frames})")
        frames = capped

    # Resolve the scene and camera to render. The adaptor defaults ('Scene' /
    # 'Camera') rarely match a real .blend, so auto-detect from the scene unless
    # overridden. Read once; both come from the same headless Blender call.
    render_scene, camera = args.render_scene, args.camera
    if (not render_scene or (not camera and not args.cameras)) and blender:
        det_scene, det_camera = read_scene_render_target(blender, scene)
        render_scene = render_scene or det_scene
        camera = camera or det_camera
        if det_scene or det_camera:
            print(f"[scene] detected scene={det_scene!r} camera={det_camera!r}")

    # Resolve the camera list. --cameras (multi) wins; else the single --camera /
    # auto-detected active camera. The template renders CameraA and CameraB as a
    # (camera x frame) task space, so two names -> both views in one job.
    if args.cameras:
        cameras = [c.strip() for c in args.cameras.split(",") if c.strip()]
    elif camera:
        cameras = [camera]
    else:
        cameras = []
    if not cameras:
        sys.stderr.write(
            "Could not determine the camera(s) to render from. Pass --cameras / "
            "--camera, or ensure the scene has an active camera and a local "
            "Blender is available for auto-detection.\n")
        return 1
    if len(cameras) > 2:
        sys.stderr.write(
            f"[note] the template renders two cameras; ignoring extras: "
            f"{cameras[2:]}\n")
    cam_a = cameras[0]
    cam_b = cameras[1] if len(cameras) > 1 else cameras[0]

    # Bake quality/color settings into a prepared copy (unless skipped). The
    # adaptor renders the scene's own settings, so this is how we apply them.
    scene_to_submit = scene
    if not args.skip_prepare:
        if not blender:
            sys.stderr.write(
                "Preparing the scene needs a local Blender but none was found.\n"
                "Pass --blender /path/to/blender, or --skip-prepare to submit "
                "the scene as-is.\n")
            return 1
        settings = {
            "engine": args.engine,
            "res_percent": args.res_percent,
            "res_x": args.res_x, "res_y": args.res_y,
            "samples": args.samples,
            "noise_threshold": args.noise_threshold,
            "light_threshold": args.light_threshold,
            "denoise": args.denoise,
            "exposure": args.exposure,
            "gamma": args.gamma,
            "view_transform": args.view_transform,
            "look": args.look,
        }
        stem = os.path.splitext(os.path.basename(scene))[0]
        prepared = os.path.join(os.path.dirname(scene), f"{stem}_prepared.blend")
        if not bake_prepared_scene(blender, scene, settings, prepared):
            return 1
        scene_to_submit = prepared
        print(f"[prepare] submitting prepared scene: {scene_to_submit}")

    job_name = args.name
    if not job_name:
        stem = os.path.splitext(os.path.basename(scene))[0]
        job_name = f"{stem}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"

    os.makedirs(args.output_dir, exist_ok=True)

    config = config_file.read_config()
    if args.profile:
        config_file.set_setting("defaults.aws_profile_name", args.profile, config)
    if args.farm_id:
        config_file.set_setting("defaults.farm_id", args.farm_id, config)
    if args.queue_id:
        config_file.set_setting("defaults.queue_id", args.queue_id, config)

    farm_id = config_file.get_setting("defaults.farm_id", config=config)
    queue_id = config_file.get_setting("defaults.queue_id", config=config)
    if not farm_id or not queue_id:
        sys.stderr.write(
            "No farm_id/queue_id configured. Set them with `deadline config set` "
            "or pass --farm-id / --queue-id.\n")
        return 1

    # Must match parameterDefinitions in deadline_bundle/template.yaml (adaptor).
    job_parameters = [
        {"name": "BlenderSceneFile", "value": scene_to_submit},
        {"name": "Frames", "value": frames},
        {"name": "CameraA", "value": cam_a},
        {"name": "CameraB", "value": cam_b},
        {"name": "OutputDir", "value": os.path.abspath(args.output_dir)},
        {"name": "Format", "value": args.format},
        {"name": "RenderEngine", "value": args.engine.lower()},
        {"name": "GpuDevice", "value": args.gpu_device},
        {"name": "CondaPackages", "value": args.conda_packages},
        {"name": "CondaChannels", "value": args.conda_channels},
    ]
    if render_scene:
        job_parameters.append({"name": "RenderScene", "value": render_scene})

    print("Submitting Blender render (adaptor) to Deadline Cloud")
    print(f"  job   : {job_name}")
    print(f"  farm  : {farm_id}")
    print(f"  queue : {queue_id}")
    print(f"  scene : {scene_to_submit}")
    print(f"  frames: {frames}  engine: {args.engine}  gpu: {args.gpu_device}")
    print(f"  render: scene={render_scene!r}  cameras={cameras!r}")

    job_id = api.create_job_from_job_bundle(
        args.bundle,
        job_parameters,
        config=config,
        name=job_name,
        max_worker_count=args.max_workers,
        priority=args.priority,
    )

    if not job_id:
        sys.stderr.write("Job submission did not return a job id (failed).\n")
        return 1

    print(f"\nSubmitted job: {job_id}")
    print(f"  deadline job get --job-id {job_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

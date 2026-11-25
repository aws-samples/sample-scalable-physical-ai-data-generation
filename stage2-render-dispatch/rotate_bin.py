"""
Randomly rotate the bin (open container `cube_open`) about its vertical axis, so
each episode sees the drop-off bin at a different yaw (domain randomization),
while guaranteeing the manipulated box can STILL be placed in the rotated bin.

Runs headless inside Blender. Operates on the scene Blender already opened (the
base twin scene) and saves the rotated result to a NEW file.

Placement constraint
--------------------
The box's final resting footprint is read from the episode recording. Because
the box is large relative to the bin, its recorded footprint already rests
against (slightly past) the bin's inner wall - so "fully inside" is impossible
for ANY rotation. Instead we require the rotation to keep the box's footprint
fitting *no worse than in the original recording*: the box's maximum reach into
the bin's local frame must not exceed its reach at 0deg (plus an optional
--fit-margin). This preserves placeability while still allowing a wide range of
yaws.

Both the bin opening and the box footprint are squares, so the valid set is
typically a broad arc; we rejection-sample a random valid angle from it.

Usage:
  blender -b blender_scene/scene_twin.blend --python rotate_bin.py -- \
      --episode episode_received_20260718_125608.json \
      --output blender_scene/binrot_scene_twin.blend \
      [--seed 12345] [--max-deg 180] [--fit-margin 0.0] [--force]
"""

import os
import sys
import math
import json
import random

import bpy
from mathutils import Matrix, Quaternion, Vector

# Unity->Blender basis change (swap Y/Z); C == C^-1. Matches episode_anim.py.
C = Matrix(((1, 0, 0, 0), (0, 0, 1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))

BIN_NAME = "cube_open"
BOX_HALF = 0.05  # Unity primitive-cube half extent (metres)


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []

    def opt(*names, default=None):
        for name in names:
            if name in argv:
                return argv[argv.index(name) + 1]
        return default

    here = os.path.dirname(os.path.abspath(__file__))
    return {
        "episode": opt("--episode", default=os.path.join(
            here, "episode_received_20260718_125608.json")),
        "output": opt("--output", default=os.path.join(
            here, "blender_scene", "binrot_scene_twin.blend")),
        "bin": opt("--bin", default=BIN_NAME),
        "seed": opt("--seed", default=None),
        "max_deg": float(opt("--max-deg", "--max_deg", default="180")),
        "fit_margin": float(opt("--fit-margin", "--fit_margin", default="0.0")),
        "min_deg": float(opt("--min-deg", "--min_deg", default="3")),
        "force": "--force" in argv,
    }


def u2b_pose(pos, quat):
    """Unity world pos+quat -> Blender world matrix (matches episode_anim.py)."""
    T = Matrix.Translation((pos["x"], pos["y"], pos["z"]))
    R = Quaternion((quat["w"], quat["x"], quat["y"], quat["z"])).to_matrix().to_4x4()
    return C @ (T @ R) @ C


def bin_opening(bin_obj):
    """Return (center_xy, inner_half) of the bin's square top opening."""
    vs = [bin_obj.matrix_world @ v.co for v in bin_obj.data.vertices]
    xs = [v.x for v in vs]
    ys = [v.y for v in vs]
    zs = [v.z for v in vs]
    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0
    ztop = max(zs)
    rim = [v for v in vs if ztop - v.z < 0.005]
    inner_half = min(
        min(abs(v.x - cx) for v in rim),
        min(abs(v.y - cy) for v in rim),
    )
    return (cx, cy), inner_half


def box_final_footprint(episode_path):
    """(bx, by, yaw) of the box's final resting pose in Blender world space."""
    ep = json.load(open(episode_path))
    s = ep["samples"][-1]
    M = u2b_pose(s["boxPosition"], s["boxRotation"])
    t = M.to_translation()
    return t.x, t.y, M.to_euler("XYZ").z


def box_reach(bx, by, yaw, cx, cy, theta):
    """Max chebyshev distance of the box's footprint corners from the bin centre,
    measured in the bin's local frame after the bin is yawed by `theta`.

    A value <= inner_half means the footprint sits within the square opening;
    larger means it protrudes past the inner wall by (reach - inner_half).
    """
    ct, st = math.cos(-theta), math.sin(-theta)
    cyaw, syaw = math.cos(yaw), math.sin(yaw)
    r = 0.0
    for sx in (-BOX_HALF, BOX_HALF):
        for sy in (-BOX_HALF, BOX_HALF):
            wx = bx + (cyaw * sx - syaw * sy)
            wy = by + (syaw * sx + cyaw * sy)
            lx = ct * (wx - cx) - st * (wy - cy)
            ly = st * (wx - cx) + ct * (wy - cy)
            r = max(r, abs(lx), abs(ly))
    return r


def main():
    args = parse_args()
    rng = random.Random(args["seed"]) if args["seed"] is not None else random.Random()

    out = args["output"]
    if os.path.exists(out) and not args["force"]:
        raise RuntimeError("refusing to overwrite existing %s (pass --force)" % out)

    bin_obj = bpy.data.objects.get(args["bin"])
    if bin_obj is None or bin_obj.type != "MESH":
        raise RuntimeError("bin object %r not found" % args["bin"])

    (cx, cy), inner_half = bin_opening(bin_obj)
    bx, by, yaw = box_final_footprint(args["episode"])
    reach0 = box_reach(bx, by, yaw, cx, cy, 0.0)
    threshold = reach0 + args["fit_margin"]
    print("[rotate-bin] bin centre=(%.4f,%.4f) inner_half=%.4f" % (cx, cy, inner_half))
    print("[rotate-bin] box final XY=(%.4f,%.4f) yaw=%.1fdeg" % (bx, by, math.degrees(yaw)))
    print("[rotate-bin] reach0=%.4f (overhang %.4f) -> fit threshold=%.4f"
          % (reach0, reach0 - inner_half, threshold))

    # Build the pool of valid yaws: fits no worse than the original recording.
    lo, hi = -args["max_deg"], args["max_deg"]
    valid = []
    d = lo
    while d <= hi:
        if abs(d) >= args["min_deg"]:  # ensure an actually visible rotation
            if box_reach(bx, by, yaw, cx, cy, math.radians(d)) <= threshold + 1e-9:
                valid.append(d)
        d += 0.5
    print("[rotate-bin] %d valid yaw candidates in [%+.0f,%+.0f] deg (>=%.0f deg)"
          % (len(valid), lo, hi, args["min_deg"]))
    if not valid:
        raise RuntimeError("no valid bin rotation preserves placement; "
                           "increase --fit-margin or lower --min-deg")

    deg = rng.choice(valid)
    theta = math.radians(deg)

    # Rotate the bin about the vertical axis through its geometric centre.
    center = Vector((cx, cy, 0.0))
    pivot = (Matrix.Translation(center) @ Matrix.Rotation(theta, 4, "Z")
             @ Matrix.Translation(-center))
    bin_obj.matrix_world = pivot @ bin_obj.matrix_world
    bpy.context.view_layer.update()

    final_reach = box_reach(bx, by, yaw, cx, cy, theta)
    print("[rotate-bin] chosen yaw = %+.1f deg" % deg)
    print("[rotate-bin] box reach after rotation = %.4f (threshold %.4f) -> %s"
          % (final_reach, threshold,
             "OK, placement preserved" if final_reach <= threshold + 1e-9 else "FAIL"))

    os.makedirs(os.path.dirname(out), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=out)
    print("[rotate-bin] saved -> %s" % out)


if __name__ == "__main__":
    main()

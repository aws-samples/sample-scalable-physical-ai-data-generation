"""
Randomize the external / over-the-shoulder context camera for one episode.

Runs headless inside Blender. Gives the stationary external camera realistic
per-episode viewpoint variation WITHOUT ever letting it point away from the task
workspace, by construction:

  1. position jitter  -- small random translation of the camera around its
     nominal camera-station position (its location in the base scene).
  2. look-at jitter   -- small random translation of the look-at point around
     the workspace centroid (the point the camera aims at in the base scene).
  3. orientation      -- NOT randomized independently. It is computed from the
     final camera position and the final look-at point, so the camera is always
     aimed at (a jittered point near) the workspace centroid.

        final_pos = nominal_pos + position_jitter
        look_at   = workspace_centroid + look_at_jitter
        orientation = look_from(final_pos -> look_at)

Randomization is seeded (default from the episode id) so a given episode always
produces the same viewpoint -- reproducible datasets.

The eye-in-hand camera is intentionally left untouched: a real wrist camera is
rigidly bolted to the gripper, so its extrinsics must not vary.

Usage
-----
  blender -b <scene.blend> --python randomize_camera.py -- \
      --camera Cam_Context --seed <episodeId> \
      --pos-jitter 0.08 --lookat-jitter 0.05 [--output out.blend]

If --output is omitted the scene is saved in place.
"""

import sys
import json
import math
import random
import hashlib

import bpy
from mathutils import Vector

# Objects whose combined footprint defines the workspace, used only as a
# fallback if the camera's view ray doesn't hit any geometry.
WORKSPACE_OBJECTS = ["Blue_box", "cube_open", "base_link"]


# --------------------------------------------------------------------------- #
# args
# --------------------------------------------------------------------------- #
def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []

    def opt(*names, default=None):
        for name in names:
            if name in argv:
                return argv[argv.index(name) + 1]
        return default

    return {
        "camera": opt("--camera", default="Cam_Context"),
        "seed": opt("--seed", "--episode-id", default=None),
        # metres of uniform +/- translation applied per axis to the camera pos
        "pos_jitter": float(opt("--pos-jitter", "--pos_jitter", default="0.08")),
        # metres of uniform +/- translation applied per axis to the look-at point
        "lookat_jitter": float(opt("--lookat-jitter", "--lookat_jitter", default="0.05")),
        "output": opt("--output", default=None),
    }


def make_rng(seed):
    """Deterministic RNG seeded from an arbitrary string (e.g. the episode id).

    A missing seed falls back to a nondeterministic RNG (system entropy)."""
    if seed is None:
        return random.Random()
    digest = hashlib.sha256(str(seed).encode("utf-8")).hexdigest()
    return random.Random(int(digest, 16))


# --------------------------------------------------------------------------- #
# geometry
# --------------------------------------------------------------------------- #
def camera_forward(cam):
    """World-space forward direction of the camera (Blender cameras look -Z)."""
    return (cam.matrix_world.to_3x3() @ Vector((0.0, 0.0, -1.0))).normalized()


def workspace_centroid(cam):
    """The point the camera is pointing at in the (base) scene.

    Primary: cast the camera's forward ray into the scene and take the hit point
    -- literally "what the camera is aimed at". Falls back to a horizontal-plane
    intersection at the tabletop height, then to the centroid of the workspace
    objects, so we always get a sensible aim point.
    """
    origin = cam.matrix_world.translation.copy()
    fwd = camera_forward(cam)

    depsgraph = bpy.context.evaluated_depsgraph_get()
    hit, location, _normal, _idx, _obj, _mat = bpy.context.scene.ray_cast(
        depsgraph, origin, fwd, distance=1000.0)
    if hit:
        return location.copy(), "ray_cast"

    # Fallback 1: intersect the view ray with the tabletop plane (z = table top).
    table = bpy.data.objects.get("Table_1_Table 1")
    if table is not None and abs(fwd.z) > 1e-6:
        corners = [table.matrix_world @ Vector(c) for c in table.bound_box]
        top_z = max(c.z for c in corners)
        t = (top_z - origin.z) / fwd.z
        if t > 0:
            return (origin + fwd * t), "table_plane"

    # Fallback 2: centroid of the workspace objects' bounding-box centres.
    pts = []
    for name in WORKSPACE_OBJECTS:
        o = bpy.data.objects.get(name)
        if o is None:
            continue
        corners = [o.matrix_world @ Vector(c) for c in o.bound_box]
        pts.append(sum(corners, Vector()) / len(corners))
    if pts:
        return (sum(pts, Vector()) / len(pts)), "object_centroid"

    # Last resort: a point 1m in front of the camera along its forward axis.
    return (origin + fwd * 1.0), "forward_1m"


def jitter_vec(rng, amount):
    """Uniform +/-amount per axis."""
    return Vector((rng.uniform(-amount, amount),
                   rng.uniform(-amount, amount),
                   rng.uniform(-amount, amount)))


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    args = parse_args()
    rng = make_rng(args["seed"])

    cam = bpy.data.objects.get(args["camera"])
    if cam is None or cam.type != "CAMERA":
        raise RuntimeError(f"camera object {args['camera']!r} not found")
    if cam.parent is not None:
        print(f"[cam-jitter] WARNING: {cam.name} is parented to "
              f"{cam.parent.name!r}; jitter is applied in world space.")

    # Nominal station + the point it currently aims at (before jitter).
    nominal_pos = cam.matrix_world.translation.copy()
    centroid, centroid_src = workspace_centroid(cam)

    # 1) jitter the camera position around its nominal station.
    final_pos = nominal_pos + jitter_vec(rng, args["pos_jitter"])
    # 2) jitter the look-at point around the workspace centroid.
    look_at = centroid + jitter_vec(rng, args["lookat_jitter"])

    # 3) derive orientation from final_pos -> look_at (never randomized directly).
    direction = (look_at - final_pos)
    if direction.length < 1e-9:
        raise RuntimeError("camera position coincides with look-at point")
    rot_quat = direction.to_track_quat("-Z", "Y")

    # Apply. Clear any parent-inverse skew by writing the world matrix directly.
    cam.rotation_mode = "QUATERNION"
    cam.location = final_pos
    cam.rotation_quaternion = rot_quat
    bpy.context.view_layer.update()

    # Machine-readable record for the augmentation manifest (parsed by run.py).
    record = {
        "camera": cam.name,
        "seed": args["seed"],
        "nominal_pos": list(nominal_pos),
        "workspace_centroid": list(centroid),
        "centroid_source": centroid_src,
        "final_pos": list(final_pos),
        "look_at": list(look_at),
        "rotation_euler_deg": [math.degrees(a) for a in rot_quat.to_euler()],
        "pos_jitter": args["pos_jitter"],
        "lookat_jitter": args["lookat_jitter"],
    }
    print("[cam-jitter] " + json.dumps(record, indent=2))
    print("CAMERA_JITTER=" + json.dumps(record))

    out = args["output"]
    if out:
        bpy.ops.wm.save_as_mainfile(filepath=out)
        print(f"[cam-jitter] saved -> {out}")
    else:
        bpy.ops.wm.save_mainfile()
        print("[cam-jitter] saved in place")


if __name__ == "__main__":
    main()

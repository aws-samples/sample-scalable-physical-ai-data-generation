"""
Fix the SO-101 wrist / eye-in-hand camera (Cam_EyeInHand) so its pose matches the
typical LeRobot SO-101 wrist-camera viewpoint.

Approach (grounded in the gripper mesh geometry, NOT the forearm):
  * The gripper fingers extend along the gripper_link LOCAL -Z axis (fingertips
    at z ~= -0.105 m; the wrist/arm sits at +Z). So the gripper APPROACH axis --
    the direction the hand reaches out to grasp -- is gripper-local -Z.
  * gripper-local +Y maps to world-up at the reference pose, so it is the natural
    camera "up" and gives a level (un-rolled) image.

We therefore mount the camera rigidly on the end-effector (gripper_link),
positioned slightly ABOVE and BEHIND the gripper, and aim it along the approach
axis with a small downward tilt. Result: the gripper jaws sit at the bottom /
centre of the frame and the workspace in front of the gripper fills the rest.

The pose is defined in the gripper's LOCAL frame and then rigidly parented, so it
rides with the arm across the whole animation with a constant extrinsic offset
(a real wrist camera is bolted on).

Usage:
  blender -b <in.blend> --python scripts/fix_wrist_camera.py -- <out.blend>
Optional overrides (metres), after the out path:
  --back B --up U --ahead A --drop D --lens MM
"""
import bpy
import sys
from mathutils import Vector, Matrix

END_EFFECTOR = "gripper_link"
CAM_NAME = "Cam_EyeInHand"

JAW_MESHES = [
    "moving_jaw_so101_v1_link_moving_jaw_so101_v1_0",
    "moving_jaw_so101_v1_link_moving_jaw_so101_v1_1",
]
BASE_MESHES = [
    "gripper_link_sts3215_03a_v1_0",
    "gripper_link_wrist_roll_follower_so101_v1_0",
    "gripper_link_wrist_roll_follower_so101_v1_1",
]

# Tunables (gripper-local metres). Defaults chosen for the SO-101 twin.
DEF = {
    "back": 0.055,   # +Z local: mount behind the jaws, toward the wrist
    "up":   0.060,   # +Y local (= world up at ref pose): mount above the gripper
    "ahead": 0.140,  # look this far BEYOND the fingertips along -Z (approach)
    "drop": 0.075,   # -Y local: lower the look-point onto the workspace (tilt down)
    "lens": 16.0,    # mm; wide FOV typical of a wrist cam
}


def cli():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not argv:
        sys.stderr.write("usage: ... fix_wrist_camera.py -- <out.blend> [--back B --up U --ahead A --drop D --lens MM]\n")
        raise SystemExit(2)
    out = argv[0]
    vals = dict(DEF)
    for k in vals:
        flag = "--" + k
        if flag in argv:
            vals[k] = float(argv[argv.index(flag) + 1])
    return out, vals


def mesh_local_verts(o, W2L, dg):
    ob = o.evaluated_get(dg)
    me = ob.to_mesh()
    mw = o.matrix_world
    vs = [W2L @ (mw @ v.co) for v in me.vertices]
    ob.to_mesh_clear()
    return vs


def fingertip_local(ee, dg):
    """Most-forward (-Z) gripper vertex in gripper-local frame => fingertip."""
    W2L = ee.matrix_world.inverted()
    allv = []
    for n in JAW_MESHES + BASE_MESHES:
        o = bpy.data.objects.get(n)
        if o and o.type == 'MESH':
            allv.extend(mesh_local_verts(o, W2L, dg))
    if not allv:
        return Vector((0.0, 0.0, -0.105))
    # fingertip = the vertex with the most negative local Z (furthest along approach)
    return min(allv, key=lambda v: v.z)


def look_at_matrix(cam_pos, target, up=Vector((0.0, 0.0, 1.0))):
    direction = (target - cam_pos)
    quat = direction.to_track_quat('-Z', 'Y')
    # bias 'up' toward the requested world up if forward is not vertical
    return Matrix.Translation(cam_pos) @ quat.to_matrix().to_4x4()


def main():
    out, v = cli()
    scene = bpy.context.scene
    ee = bpy.data.objects.get(END_EFFECTOR)
    if ee is None:
        raise RuntimeError(f"end-effector {END_EFFECTOR!r} not found")

    # Reference pose = first frame (shoulder forward, elbow flexed, wrist ~90 deg).
    scene.frame_set(scene.frame_start)
    bpy.context.view_layer.update()

    dg = bpy.context.evaluated_depsgraph_get()
    tip = fingertip_local(ee, dg)          # local, ~ (0, +0.01, -0.105)
    approach_z = tip.z                     # negative

    # Camera pose defined in gripper-LOCAL frame.
    cam_pos_local = Vector((0.0, v["up"], v["back"]))
    # Look point: beyond the fingertips along -Z, dropped down (-Y) onto the workspace.
    look_local = Vector((0.0, tip.y - v["drop"], approach_z - v["ahead"]))

    M = ee.matrix_world.copy()
    cam_pos_world = M @ cam_pos_local
    look_world = M @ look_local

    # Get or (re)create the camera object.
    cam = bpy.data.objects.get(CAM_NAME)
    if cam is None or cam.type != 'CAMERA':
        cam_data = bpy.data.cameras.new(CAM_NAME)
        cam = bpy.data.objects.new(CAM_NAME, cam_data)
        scene.collection.objects.link(cam)
    cam.data.lens = v["lens"]
    cam.data.clip_start = 0.01
    cam.data.clip_end = 100.0

    # Place in world, then rigidly parent to the end-effector.
    cam.parent = None
    cam.matrix_world = look_at_matrix(cam_pos_world, look_world)
    bpy.context.view_layer.update()
    cam.parent = ee
    cam.matrix_parent_inverse = M.inverted()
    bpy.context.view_layer.update()

    # Report.
    fwd = (cam.matrix_world.to_3x3() @ Vector((0, 0, -1))).normalized()
    approach_world = (M.to_3x3() @ Vector((0, 0, -1))).normalized()  # local -Z in world
    print("[fix-wrist-cam] params:", v)
    print(f"[fix-wrist-cam] fingertip(local)      = ({tip.x:+.4f},{tip.y:+.4f},{tip.z:+.4f})")
    print(f"[fix-wrist-cam] cam pos(world)         = {tuple(round(x,4) for x in cam_pos_world)}")
    print(f"[fix-wrist-cam] optical axis (world)   = ({fwd.x:+.3f},{fwd.y:+.3f},{fwd.z:+.3f})")
    print(f"[fix-wrist-cam] approach axis (world)  = ({approach_world.x:+.3f},{approach_world.y:+.3f},{approach_world.z:+.3f})")
    print(f"[fix-wrist-cam] axis . approach        = {fwd.dot(approach_world):+.3f}  (1.0 = perfectly along approach)")

    bpy.ops.wm.save_as_mainfile(filepath=out)
    print(f"[fix-wrist-cam] saved -> {out}")


if __name__ == "__main__":
    main()

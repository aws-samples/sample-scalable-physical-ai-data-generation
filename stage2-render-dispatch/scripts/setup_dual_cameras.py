"""
Apply a dual-camera configuration to an SO-101 twin scene (runs inside Blender).

  1. Cam_EyeInHand  -- "eye-in-hand": a camera rigidly mounted just behind the
     gripper jaws, parented to `gripper_link`, looking down the approach axis at
     the grasp point. Rides with the arm across the whole animation.
  2. Cam_Context    -- stationary "over-the-shoulder" external context camera
     (the scene's existing external camera, kept in place and renamed for
     clarity). Remains the scene's active/default render camera.

Both cameras persist in the .blend; the render pipeline selects one by name via
`submit_to_deadline.py --camera <name>` (default = scene's active camera).

Usage:
  blender -b <in.blend> --python setup_dual_cameras.py -- <out.blend>
"""
import bpy
import sys
import math
from mathutils import Vector

# --- tunables --------------------------------------------------------------
END_EFFECTOR = "gripper_link"           # empty the eye-in-hand cam is bolted to
GRASP_MARKER = "moving_jaw_so101_v1_link"  # defines the "forward" (approach) axis
EYE_LENS_MM = 18.0                       # wide FOV for close-up hand view
EYE_BACK = 0.10                          # metres behind the jaws (along -approach)
EYE_UP = 0.05                            # metres above the hand (world up)
EYE_LOOK_AHEAD = 0.10                    # aim this far past the jaws
CONTEXT_LENS_MM = None                   # None = keep existing lens

# Context camera placement: stand it in FRONT of the robot, on the opposite side
# of the table, looking back across the workspace toward the robot. Computed
# from the scene (robot base -> workspace direction), so it survives small scene
# changes without hard-coded coordinates.
ROBOT_BASE = "base_link"                 # robot base empty (near table edge)
TABLE_OBJ = "Table_1_Table 1"            # table mesh (defines the far edge)
WORKSPACE_OBJECTS = [                     # manipulation area the camera frames
    "Blue_box", "cube_open", "moving_jaw_so101_v1_link", "gripper_link",
]
CONTEXT_STANDOFF = 0.25                   # metres beyond the far table edge
CONTEXT_HEIGHT = 0.50                     # metres above the table top
CONTEXT_LOOK_UP = 0.03                    # aim this far above the workspace centroid
CONTEXT_PITCH_DOWN_DEG = 5.0              # extra downward tilt of the context cam only


def out_path():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if not argv:
        sys.stderr.write("usage: ... --python setup_dual_cameras.py -- <out.blend>\n")
        raise SystemExit(2)
    return argv[0]


def look_at_matrix(cam_pos, target, up=Vector((0.0, 0.0, 1.0)), pitch_down_deg=0.0):
    """World matrix for a camera at cam_pos aimed at target (Blender -Z forward).

    pitch_down_deg > 0 tilts the optical axis further DOWN by that many degrees,
    rotating about the camera's LOCAL X (right) axis so only the pitch changes
    (the aim direction and roll are otherwise preserved).
    """
    direction = (target - cam_pos)
    from mathutils import Matrix
    quat = direction.to_track_quat('-Z', 'Y')
    mat = Matrix.Translation(cam_pos) @ quat.to_matrix().to_4x4()
    if pitch_down_deg:
        # Local -X rotation pitches the -Z forward axis downward (nose-down).
        mat = mat @ Matrix.Rotation(math.radians(-pitch_down_deg), 4, 'X')
    return mat


def find_existing_external_camera():
    """Prefer the scene's active camera; else the first non-parented camera."""
    sc = bpy.context.scene
    if sc.camera and sc.camera.parent is None:
        return sc.camera
    for ob in bpy.data.objects:
        if ob.type == 'CAMERA' and ob.parent is None:
            return ob
    return sc.camera


def _obj_center(o):
    """World-space centre of an object (bbox centre for meshes, origin else)."""
    if o.type == 'MESH':
        cs = [o.matrix_world @ Vector(c) for c in o.bound_box]
        return sum(cs, Vector()) / len(cs)
    return o.matrix_world.translation.copy()


def workspace_centroid():
    """Centroid of the manipulation area (box / container / gripper), falling
    back to the table centre if none of those objects are present."""
    pts = []
    for name in WORKSPACE_OBJECTS:
        o = bpy.data.objects.get(name)
        if o is not None:
            pts.append(_obj_center(o))
    if pts:
        return sum(pts, Vector()) / len(pts)
    table = bpy.data.objects.get(TABLE_OBJ)
    if table is not None:
        return _obj_center(table)
    return Vector((0.0, 0.0, 0.0))


def table_top_z():
    """Max world Z of the table mesh, or None if the table isn't found."""
    table = bpy.data.objects.get(TABLE_OBJ)
    if table is not None and table.type == 'MESH':
        cs = [table.matrix_world @ Vector(c) for c in table.bound_box]
        return max(c.z for c in cs)
    return None


def place_context_camera(cam):
    """Position `cam` in front of the robot, on the opposite side of the table,
    aimed back across the workspace toward the robot.

    Direction is derived from robot_base -> workspace_centroid (the way the robot
    faces), so the camera sits past the FAR table edge along that direction. Only
    the camera's world matrix is set; its lens/sensor are left untouched.
    """
    centroid = workspace_centroid()
    base = bpy.data.objects.get(ROBOT_BASE)
    base_pos = base.matrix_world.translation.copy() if base else \
        Vector((centroid.x, centroid.y - 1.0, centroid.z))

    # Horizontal "front of robot" direction (robot faces the workspace).
    d = Vector((centroid.x - base_pos.x, centroid.y - base_pos.y, 0.0))
    if d.length < 1e-6:
        d = Vector((0.0, 1.0, 0.0))
    d.normalize()

    # Distance from the workspace centroid out to the far edge of the table,
    # measured along d (so we clear the table regardless of its extent).
    far = 0.0
    table = bpy.data.objects.get(TABLE_OBJ)
    if table is not None and table.type == 'MESH':
        c0 = Vector((centroid.x, centroid.y, 0.0))
        for c in table.bound_box:
            wc = table.matrix_world @ Vector(c)
            far = max(far, (Vector((wc.x, wc.y, 0.0)) - c0).dot(d))

    top_z = table_top_z()
    if top_z is None:
        top_z = centroid.z

    cam_xy = Vector((centroid.x, centroid.y, 0.0)) + d * (far + CONTEXT_STANDOFF)
    cam_pos = Vector((cam_xy.x, cam_xy.y, top_z + CONTEXT_HEIGHT))
    look_at = Vector((centroid.x, centroid.y, centroid.z + CONTEXT_LOOK_UP))

    cam.parent = None
    cam.matrix_world = look_at_matrix(cam_pos, look_at,
                                      pitch_down_deg=CONTEXT_PITCH_DOWN_DEG)
    bpy.context.view_layer.update()
    print("[dual-cam] Cam_Context placed in front of robot "
          f"(+{CONTEXT_PITCH_DOWN_DEG:g} deg downward pitch): "
          "pos=(%.3f,%.3f,%.3f) look_at=(%.3f,%.3f,%.3f)"
          % (cam_pos.x, cam_pos.y, cam_pos.z, look_at.x, look_at.y, look_at.z))
    return cam_pos, look_at


def make_eye_in_hand(scene):
    ee = bpy.data.objects.get(END_EFFECTOR)
    jaw = bpy.data.objects.get(GRASP_MARKER)
    if ee is None:
        raise RuntimeError(f"end-effector object {END_EFFECTOR!r} not found")
    if jaw is None:
        jaw = ee

    scene.frame_set(scene.frame_start)
    bpy.context.view_layer.update()

    ee_pos = ee.matrix_world.translation.copy()
    jaw_pos = jaw.matrix_world.translation.copy()
    forward = (jaw_pos - ee_pos)
    if forward.length < 1e-6:
        # fall back to the end-effector local +Y as the approach axis
        forward = (ee.matrix_world.to_3x3() @ Vector((0.0, 1.0, 0.0)))
    forward.normalize()

    cam_pos = ee_pos - forward * EYE_BACK + Vector((0.0, 0.0, 1.0)) * EYE_UP
    target = jaw_pos + forward * EYE_LOOK_AHEAD

    cam_data = bpy.data.cameras.new("Cam_EyeInHand")
    cam_data.lens = EYE_LENS_MM
    cam_data.clip_start = 0.01           # see objects right at the jaws
    cam_data.clip_end = 100.0
    cam = bpy.data.objects.new("Cam_EyeInHand", cam_data)
    scene.collection.objects.link(cam)

    # place in world, then rigidly parent to the end-effector (rides with arm)
    cam.parent = None
    cam.matrix_world = look_at_matrix(cam_pos, target)
    bpy.context.view_layer.update()
    cam.parent = ee
    cam.matrix_parent_inverse = ee.matrix_world.inverted()
    bpy.context.view_layer.update()
    return cam


def main():
    scene = bpy.context.scene

    # 1) context camera: reuse the existing external cam object (keeps its lens/
    #    sensor), but REPOSITION it in front of the robot on the opposite side of
    #    the table, looking back across the workspace.
    context_cam = find_existing_external_camera()
    if context_cam is None:
        cam_data = bpy.data.cameras.new("Cam_Context")
        context_cam = bpy.data.objects.new("Cam_Context", cam_data)
        scene.collection.objects.link(context_cam)
    context_cam.name = "Cam_Context"
    if context_cam.data is not None:
        context_cam.data.name = "Cam_Context"
        if CONTEXT_LENS_MM:
            context_cam.data.lens = CONTEXT_LENS_MM
    place_context_camera(context_cam)

    # 2) eye-in-hand camera
    eye = make_eye_in_hand(scene)

    # keep the stationary context view as the default render camera
    scene.camera = context_cam if context_cam is not None else eye

    print("[dual-cam] cameras in scene:")
    for ob in bpy.data.objects:
        if ob.type == 'CAMERA':
            t = ob.matrix_world.translation
            print("  %-16s parent=%-14s lens=%.1f pos=(%.3f,%.3f,%.3f)%s"
                  % (ob.name, ob.parent.name if ob.parent else '-',
                     ob.data.lens, t.x, t.y, t.z,
                     '  [ACTIVE]' if ob is scene.camera else ''))

    dst = out_path()
    bpy.ops.wm.save_as_mainfile(filepath=dst)
    print(f"[dual-cam] saved -> {dst}")


if __name__ == "__main__":
    main()

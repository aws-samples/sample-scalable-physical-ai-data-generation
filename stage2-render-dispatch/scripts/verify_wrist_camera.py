"""Verify Cam_EyeInHand is rigidly attached to the end-effector and aimed along
the gripper approach axis. Rotates a proximal joint and checks the camera's
pose RELATIVE to gripper_link stays constant (rigid mount)."""
import bpy
from mathutils import Vector, Euler

sc = bpy.context.scene
ee = bpy.data.objects["gripper_link"]
cam = bpy.data.objects["Cam_EyeInHand"]

print("parent:", cam.parent.name if cam.parent else None)
assert cam.parent is ee, "camera is not parented to gripper_link"

def rel():
    return ee.matrix_world.inverted() @ cam.matrix_world

def axis_dot():
    fwd = (cam.matrix_world.to_3x3() @ Vector((0,0,-1))).normalized()
    approach = (ee.matrix_world.to_3x3() @ Vector((0,0,-1))).normalized()  # local -Z
    return fwd.dot(approach)

sc.frame_set(sc.frame_start); bpy.context.view_layer.update()
r0 = rel()
print(f"rel translation @ref = {tuple(round(x,4) for x in r0.translation)}")
print(f"optical.approach @ref = {axis_dot():+.3f}")

# Perturb a proximal joint (shoulder) to simulate arm motion, check rigidity.
sh = bpy.data.objects.get("shoulder_link")
saved = sh.rotation_euler.copy() if sh else None
worst = 0.0
if sh:
    for ang in (0.3, -0.4, 0.8):
        sh.rotation_euler = Euler((saved.x, saved.y, saved.z + ang))
        bpy.context.view_layer.update()
        r = rel()
        d = max(abs(a-b) for a,b in zip(sum([list(row) for row in r], []),
                                        sum([list(row) for row in r0], [])))
        worst = max(worst, d)
        print(f"  shoulder +{ang:+.2f}rad: rel-matrix max delta vs ref = {d:.2e}  optical.approach={axis_dot():+.3f}")
    sh.rotation_euler = saved
    bpy.context.view_layer.update()
print(f"RIGID: worst relative-matrix drift = {worst:.2e} (should be ~0)")

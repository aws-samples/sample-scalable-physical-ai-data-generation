import bpy
from mathutils import Vector

sc = bpy.context.scene
print(f"active camera: {sc.camera.name if sc.camera else None}")
print("cameras:")
for ob in bpy.data.objects:
    if ob.type == 'CAMERA':
        print(f"  {ob.name:16s} parent={ob.parent.name if ob.parent else '-':14s} lens={ob.data.lens}")

eye = bpy.data.objects.get("Cam_EyeInHand")
ee = bpy.data.objects.get("gripper_link")
jaw = bpy.data.objects.get("moving_jaw_so101_v1_link")

print("\neye-in-hand tracking check (should stay ~constant offset to gripper):")
for f in [sc.frame_start, (sc.frame_start+sc.frame_end)//2, sc.frame_end]:
    sc.frame_set(f)
    bpy.context.view_layer.update()
    cam_p = eye.matrix_world.translation
    ee_p = ee.matrix_world.translation
    jaw_p = jaw.matrix_world.translation
    offset = (cam_p - ee_p).length
    # is the camera looking toward the jaw? forward = -Z of camera
    fwd = (eye.matrix_world.to_3x3() @ Vector((0,0,-1))).normalized()
    to_jaw = (jaw_p - cam_p).normalized()
    aim_dot = fwd.dot(to_jaw)
    print(f"  f{f:>4}: cam=({cam_p.x:.3f},{cam_p.y:.3f},{cam_p.z:.3f}) "
          f"|cam-gripper|={offset:.4f}m  aim·toJaw={aim_dot:.3f}")

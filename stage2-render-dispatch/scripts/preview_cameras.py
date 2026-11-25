import bpy, os, sys

outdir = sys.argv[sys.argv.index("--")+1] if "--" in sys.argv else "."
sc = bpy.context.scene
sc.frame_set(120)
sc.render.engine = 'BLENDER_EEVEE'
sc.render.resolution_percentage = 25
sc.render.image_settings.file_format = 'PNG'
for name in ["Cam_Context", "Cam_EyeInHand"]:
    cam = bpy.data.objects.get(name)
    if not cam:
        continue
    sc.camera = cam
    sc.render.filepath = os.path.join(outdir, f"preview_{name}.png")
    bpy.ops.render.render(write_still=True)
    print(f"[preview] wrote preview_{name}.png")

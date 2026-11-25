import bpy, os, sys
outdir = sys.argv[sys.argv.index("--")+1] if "--" in sys.argv else "."
sc = bpy.context.scene
sc.camera = bpy.data.objects["Cam_Context"]
sc.frame_set(120)
sc.render.engine = 'BLENDER_EEVEE'
sc.render.resolution_percentage = 25
sc.render.image_settings.file_format = 'PNG'
sc.render.filepath = os.path.join(outdir, "preview_Cam_Context_jittered.png")
bpy.ops.render.render(write_still=True)
print("[preview] wrote preview_Cam_Context_jittered.png")

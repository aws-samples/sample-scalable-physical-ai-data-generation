import bpy, os, sys

argv = sys.argv[sys.argv.index("--")+1:] if "--" in sys.argv else []
outdir = argv[0] if argv else "."
frames = [int(x) for x in argv[1:]] if len(argv) > 1 else [1, 120, 250]

sc = bpy.context.scene
# Pick an available EEVEE engine name (Blender 4 vs 5).
for eng in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
    try:
        sc.render.engine = eng
        break
    except Exception:
        continue
sc.render.resolution_x = 640
sc.render.resolution_y = 480
sc.render.resolution_percentage = 100
sc.render.image_settings.file_format = 'PNG'

cam = bpy.data.objects.get("Cam_EyeInHand")
if not cam:
    raise SystemExit("Cam_EyeInHand not found")
sc.camera = cam
for f in frames:
    sc.frame_set(f)
    sc.render.filepath = os.path.join(outdir, f"wrist_f{f:03d}.png")
    bpy.ops.render.render(write_still=True)
    print(f"[preview] wrote wrist_f{f:03d}.png")

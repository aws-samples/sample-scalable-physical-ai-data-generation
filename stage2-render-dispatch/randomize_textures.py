"""
Randomly pick a texture set for the table AND for the box, apply each to its
material in blender_scene/scene_twin.blend, and save the result as a new .blend
whose name contains both chosen texture names.

Run headless:
    /Applications/Blender.app/Contents/MacOS/Blender \
        --background blender_scene/scene_twin.blend \
        --python randomize_texture_in_blender_scene.py

Point at different texture directories (both --table-dir and --table_dir work):
    ... --python randomize_texture_in_blender_scene.py -- \
        --table-dir blender_texture/table_texture \
        --box-dir   blender_texture/box_texture

Force a specific set instead of a random pick (matches by substring of the
zip filename, e.g. --table wood_planks picks wood_planks_4k.blend.zip):
    ... -- --table wood_planks --box metal_0083

Also randomize the table size (default OFF — the table is only resized when
this flag is present; it always stays large enough to hold the robot, basket,
and box):
    ... -- --randomize-table

Lighting is gently randomized by default (small intensity + tint jitter, never
drastic). Disable it with:
    ... -- --randomize-light false
"""

import os
import sys
import glob
import math
import random
import shutil
import zipfile
import tempfile

import bpy

# --- Paths (resolved relative to this script, not the CWD) --------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TABLE_DIR = os.path.join(SCRIPT_DIR, "blender_texture", "table_texture")
BOX_DIR = os.path.join(SCRIPT_DIR, "blender_texture", "box_texture")
SCENE_DIR = os.path.join(SCRIPT_DIR, "blender_scene")
ORIGINAL_SCENE = "scene_twin.blend"  # never delete this one

# object -> material to rebuild
TABLE_OBJECT = "Table_1_Table 1"
TABLE_MATERIAL = "table"
BOX_OBJECT = "cube_open"   # the open basket/bin on the table
BOX_MATERIAL = "cube"

# The two yellow UI buttons in the table corners — removed before rendering.
BUTTON_OBJECTS = ["TableButton", "CancelButton"]


def parse_args():
    """Read args after the '--' separator that Blender ignores.

    Accepts both dash and underscore spellings (--table-dir / --table_dir).
    """
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []

    def opt(*names, default=None):
        for name in names:
            if name in argv:
                return argv[argv.index(name) + 1]
        return default

    def flag(*names):
        """Boolean flag; present -> True (also accepts '--flag true/false')."""
        for name in names:
            if name in argv:
                val = argv[argv.index(name) + 1] if argv.index(name) + 1 < len(argv) else ""
                if val.lower() in ("false", "0", "no"):
                    return False
                return True
        return False

    return {
        "table_dir": os.path.abspath(opt("--table-dir", "--table_dir", default=TABLE_DIR)),
        "box_dir": os.path.abspath(opt("--box-dir", "--box_dir", default=BOX_DIR)),
        "table_forced": opt("--table"),
        "box_forced": opt("--box"),
        # Off by default: only resize the table when explicitly asked.
        "randomize_table": flag("--randomize-table", "--randomize_table"),
        # On by default: gently vary the lighting. Pass '--randomize-light false' to disable.
        "randomize_light": opt("--randomize-light", "--randomize_light",
                               default="true").lower() not in ("false", "0", "no"),
    }


def choose_texture_zip(texture_dir, forced=None):
    """Pick a *.zip texture set (covers both *.blend.zip and plain *.zip)."""
    zips = sorted(glob.glob(os.path.join(texture_dir, "*.zip")))
    if not zips:
        raise RuntimeError(f"No *.zip texture sets found in {texture_dir}")
    if forced:
        for z in zips:
            if forced in os.path.basename(z):
                return z
        raise RuntimeError(f"Requested texture '{forced}' not found among: {zips}")
    return random.choice(zips)


def texture_name_from_zip(zip_path):
    """Strip known suffixes to a clean label used in the output filename.

    'wood_planks_4k.blend.zip'   -> 'wood_planks_4k'
    'metal_0083_1k_r9ZJJl.zip'   -> 'metal_0083_1k'  (drop random hash token)
    """
    name = os.path.basename(zip_path)
    for suffix in (".blend.zip", ".zip"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    # Drop a trailing random-hash token like the '_r9ZJJl' in AmbientCG names.
    parts = name.split("_")
    if len(parts) > 1 and parts[-1].isalnum() and any(c.isupper() for c in parts[-1]):
        parts = parts[:-1]
    return "_".join(parts)


def classify_map(filename):
    """Map a texture filename to a PBR channel, or None if it's not a map we
    use (previews, AO, keywords, descriptions, DirectX normals, etc.)."""
    lower = filename.lower()
    if not lower.endswith((".jpg", ".jpeg", ".png", ".exr")):
        return None
    if "preview" in lower or "thumb" in lower:
        return None
    # Order matters: check specific tokens before generic ones.
    if "metallic" in lower or "metalness" in lower:
        return "metallic"
    if "rough" in lower:
        return "roughness"
    if "nor" in lower:
        if "directx" in lower or "_direct" in lower:  # prefer OpenGL normals
            return None
        return "normal"
    if "disp" in lower or "height" in lower:
        return "displacement"
    if "diff" in lower or "albedo" in lower or "color" in lower or "_col" in lower:
        return "diffuse"
    return None  # ao, opacity, etc. — ignored


def extract_textures(zip_path, dest_dir):
    """Extract image maps from the zip (any internal layout); return
    {channel: filepath}. First good match per channel wins."""
    maps = {}
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            channel = classify_map(os.path.basename(member))
            if channel is None or channel in maps:
                continue
            zf.extract(member, dest_dir)
            maps[channel] = os.path.join(dest_dir, member)
    if "diffuse" not in maps:
        raise RuntimeError(f"No diffuse/albedo/color map found inside {zip_path}")
    return maps


def load_image(path, non_color):
    img = bpy.data.images.load(path, check_existing=True)
    if non_color:
        img.colorspace_settings.name = "Non-Color"
    img.pack()  # embed pixels so the saved .blend is self-contained
    return img


def rebuild_material(material_name, maps, use_uv, disp_scale=0.02):
    """Rebuild a material as a Principled BSDF wired to the given PBR maps.

    use_uv=True  -> texture coordinates come from the mesh UV map.
    use_uv=False -> object has no UVs, so use Generated coords with Box
                    projection so the texture still shows up.
    """
    mat = bpy.data.materials.get(material_name)
    if mat is None:
        raise RuntimeError(f"Material '{material_name}' not found in scene")

    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()

    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (600, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (200, 0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    tex_coord = nt.nodes.new("ShaderNodeTexCoord")
    tex_coord.location = (-1000, 0)
    mapping = nt.nodes.new("ShaderNodeMapping")
    mapping.location = (-800, 0)
    coord_socket = "UV" if use_uv else "Generated"
    nt.links.new(tex_coord.outputs[coord_socket], mapping.inputs["Vector"])

    def image_node(path, non_color, y):
        node = nt.nodes.new("ShaderNodeTexImage")
        node.location = (-500, y)
        node.image = load_image(path, non_color)
        if not use_uv:
            # Box projection covers all faces without a UV unwrap.
            node.projection = "BOX"
            node.projection_blend = 0.2
        nt.links.new(mapping.outputs["Vector"], node.inputs["Vector"])
        return node

    # Base color
    diff = image_node(maps["diffuse"], non_color=False, y=400)
    nt.links.new(diff.outputs["Color"], bsdf.inputs["Base Color"])

    # Metallic
    if "metallic" in maps:
        metal = image_node(maps["metallic"], non_color=True, y=150)
        nt.links.new(metal.outputs["Color"], bsdf.inputs["Metallic"])

    # Roughness
    if "roughness" in maps:
        rough = image_node(maps["roughness"], non_color=True, y=-100)
        nt.links.new(rough.outputs["Color"], bsdf.inputs["Roughness"])

    # Normal
    if "normal" in maps:
        nor = image_node(maps["normal"], non_color=True, y=-350)
        nmap = nt.nodes.new("ShaderNodeNormalMap")
        nmap.location = (-150, -350)
        nt.links.new(nor.outputs["Color"], nmap.inputs["Color"])
        nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

    # Displacement (only meaningful with a UV map; skip for box projection)
    if "displacement" in maps and use_uv:
        disp_img = image_node(maps["displacement"], non_color=True, y=-600)
        disp = nt.nodes.new("ShaderNodeDisplacement")
        disp.location = (200, -600)
        disp.inputs["Scale"].default_value = disp_scale
        nt.links.new(disp_img.outputs["Color"], disp.inputs["Height"])
        nt.links.new(disp.outputs["Displacement"], out.inputs["Displacement"])

    return mat


def apply_set(label, texture_dir, forced, obj_name, mat_name, tmp_dirs,
              optional=False):
    """Choose, extract, and apply one texture set. Returns the clean name, or
    None if the surface was optional and skipped.

    `optional=True` surfaces are skipped (with a warning) when the target
    material is not in the scene or no texture zips were supplied, instead of
    failing the whole randomization pass. Required surfaces still raise -- a
    missing table material means the wrong scene was uploaded, and that should
    fail loudly rather than render a grey table.
    """
    if optional:
        if bpy.data.materials.get(mat_name) is None:
            print(f"[randomize] {label}: material '{mat_name}' not in this scene; "
                  f"skipping this surface.")
            return None
        if not glob.glob(os.path.join(texture_dir, "*.zip")):
            print(f"[randomize] {label}: no *.zip texture sets in {texture_dir}; "
                  f"skipping this surface.")
            return None
    zip_path = choose_texture_zip(texture_dir, forced)
    name = texture_name_from_zip(zip_path)
    print(f"[randomize] {label}: selected '{name}'")

    obj = bpy.data.objects.get(obj_name)
    if obj is None:
        print(f"[randomize] WARNING: object '{obj_name}' not found; "
              f"applying to material '{mat_name}' anyway.")
        use_uv = True
    else:
        use_uv = len(obj.data.uv_layers) > 0
        if not use_uv:
            print(f"[randomize] '{obj_name}' has no UV map; using Box projection.")

    tmp_dir = tempfile.mkdtemp(prefix=f"{label}_tex_")
    tmp_dirs.append(tmp_dir)
    maps = extract_textures(zip_path, tmp_dir)
    print(f"[randomize] {label} maps: "
          f"{ {k: os.path.basename(v) for k, v in maps.items()} }")

    rebuild_material(mat_name, maps, use_uv=use_uv)
    return name


def _world_aabb(obj):
    """World-space axis-aligned bounding box as (minx,maxx,miny,maxy,minz,maxz)."""
    from mathutils import Vector
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    xs = [v.x for v in corners]
    ys = [v.y for v in corners]
    zs = [v.z for v in corners]
    return (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))


def randomize_table_size():
    """Randomly rescale the table in the horizontal (X/Y) plane while keeping
    the tabletop height fixed and ensuring the resulting top still covers the
    full footprint of everything resting on it (robot, basket, box).

    Returns a scale factor for logging.
    """
    table = bpy.data.objects.get(TABLE_OBJECT)
    if table is None:
        print(f"[randomize] table object '{TABLE_OBJECT}' not found; skipping resize.")
        return 1.0

    tb = _world_aabb(table)
    top_z = tb[5]

    # The table may be rotated, so a local axis can map to world-vertical.
    # Find the two LOCAL axes that lie in the world horizontal plane (their
    # world direction is least vertical); we scale only those, leaving the
    # vertical/height axis untouched so the tabletop stays at the same Z.
    verticality = []  # (|world_z component|, local_axis_index)
    for i in range(3):
        world_dir = table.matrix_world.col[i].to_3d().normalized()
        verticality.append((abs(world_dir.z), i))
    verticality.sort()
    horizontal_axes = [verticality[0][1], verticality[1][1]]  # two least-vertical

    # Footprint of everything that must stay supported by the tabletop.
    ENV_PREFIX = "Template_Environment"
    skip = {TABLE_OBJECT, "Grid", "TableButton", "CancelButton"}
    pminx = pmaxx = pminy = pmaxy = None
    for o in bpy.data.objects:
        if o.type != "MESH" or o.name in skip or o.name.startswith(ENV_PREFIX):
            continue
        a = _world_aabb(o)
        # Only count things resting on/above the tabletop, not floor props.
        if a[5] < top_z - 0.25:
            continue
        pminx = a[0] if pminx is None else min(pminx, a[0])
        pmaxx = a[1] if pmaxx is None else max(pmaxx, a[1])
        pminy = a[2] if pminy is None else min(pminy, a[2])
        pmaxy = a[3] if pmaxy is None else max(pmaxy, a[3])

    # Table center in the horizontal plane (scaling pivots about the origin).
    ox, oy = table.matrix_world.translation.x, table.matrix_world.translation.y

    # Smallest uniform horizontal scale that still keeps the payload within the
    # tabletop, given we scale about the table's origin. margin gives breathing
    # room so nothing sits exactly on the edge.
    margin = 0.9  # payload must fit inside 90% of the (scaled) table half-extents
    min_scale = 0.5
    if pminx is not None:
        half_x = (tb[1] - tb[0]) / 2.0
        half_y = (tb[3] - tb[2]) / 2.0
        need_x = max(abs(pmaxx - ox), abs(ox - pminx))
        need_y = max(abs(pmaxy - oy), abs(oy - pminy))
        # scaled_half * margin >= need  ->  scale >= need / (half * margin)
        min_scale = max(need_x / (half_x * margin), need_y / (half_y * margin))
        min_scale = min(min_scale, 1.0)  # never forced to grow

    # Pick a factor in [min_scale, 1.4]; can shrink to the payload limit or grow.
    lo = round(min_scale + 0.02, 3)  # tiny buffer above the hard minimum
    hi = 1.4
    if lo >= hi:
        lo = hi  # payload already fills the table; only allow growth
    factor = random.uniform(lo, hi)

    # Scale only the two horizontal local axes; leave the vertical axis so the
    # tabletop height and thickness stay put.
    for axis in horizontal_axes:
        table.scale[axis] *= factor
    bpy.context.view_layer.update()
    print(f"[randomize] table resized by factor {factor:.3f} "
          f"(min allowed {lo:.3f}) — keeps robot/basket/box supported")
    return factor


# Discrete color-temperature presets the Sun can be tinted with (linear RGB).
WARM_LIGHT = (1.00, 0.82, 0.62)     # ~3200K, incandescent / late-afternoon
NEUTRAL_LIGHT = (1.00, 1.00, 1.00)  # ~5500K, midday white
COOL_LIGHT = (0.75, 0.85, 1.00)     # ~7500K, overcast / shade

# Sun direction ranges (radians). x = elevation tilt, y = side tilt, z = azimuth.
SUN_ELEV_MIN, SUN_ELEV_MAX = math.radians(15), math.radians(75)
SUN_TILT_MIN, SUN_TILT_MAX = math.radians(-20), math.radians(20)

# Multiplicative trim for non-SUN lights, applied to whatever the scene already
# specifies rather than to an absolute figure — the twin's ceiling fixtures are
# tuned to light the room, so they want nudging, not replacing.
FIXTURE_TRIM_MIN, FIXTURE_TRIM_MAX = 0.75, 1.25


def randomize_lighting():
    """Randomize the scene's lights plus world ambient strength.

    A SUN is treated as a sun: intensity, direction, colour temperature, and
    soft-shadow angle. Anything else — the twin's ceiling fluorescent AREA
    lights — only has its output trimmed and tinted, because re-aiming a ceiling
    panel the way you'd swing a sun points it at a wall and lights nothing.

    Branching on type is also what keeps this working on a scene with no SUN:
    `.angle` exists only on a sun, so reading it unconditionally (even just to
    log it) raises AttributeError on an AreaLight.
    """
    changed = []
    for o in bpy.data.objects:
        if o.type != "LIGHT":
            continue
        L = o.data
        if L.type == "SUN":
            # Absolute intensity in a wide band (irradiance for a SUN).
            L.energy = random.uniform(0.5, 2.0)
            # Full azimuth, varied elevation and a little side tilt.
            o.rotation_euler.x = random.uniform(SUN_ELEV_MIN, SUN_ELEV_MAX)
            o.rotation_euler.y = random.uniform(SUN_TILT_MIN, SUN_TILT_MAX)
            o.rotation_euler.z = random.uniform(0.0, 2.0 * math.pi)
            L.color = random.choice([WARM_LIGHT, NEUTRAL_LIGHT, COOL_LIGHT])
            # Soft-shadow angular size: 0.1deg (crisp) .. 20deg (very soft).
            L.angle = math.radians(random.uniform(0.1, 20.0))
            changed.append(
                f"{o.name} SUN energy={L.energy:.2f} "
                f"rot=({math.degrees(o.rotation_euler.x):.1f},"
                f"{math.degrees(o.rotation_euler.y):.1f},"
                f"{math.degrees(o.rotation_euler.z):.1f})deg "
                f"color=({L.color[0]:.2f},{L.color[1]:.2f},{L.color[2]:.2f}) "
                f"angle={math.degrees(L.angle):.1f}deg"
            )
        else:
            L.energy = max(0.05, L.energy * random.uniform(FIXTURE_TRIM_MIN,
                                                           FIXTURE_TRIM_MAX))
            L.color = random.choice([WARM_LIGHT, NEUTRAL_LIGHT, COOL_LIGHT])
            changed.append(
                f"{o.name} {L.type} energy={L.energy:.2f} "
                f"color=({L.color[0]:.2f},{L.color[1]:.2f},{L.color[2]:.2f})"
            )

    # World ambient: keep within a tight band so it never blows out or crushes.
    for w in bpy.data.worlds:
        if w.use_nodes:
            for n in w.node_tree.nodes:
                if n.type == "BACKGROUND":
                    s = n.inputs["Strength"]
                    s.default_value = max(0.4, min(0.9, s.default_value * random.uniform(0.85, 1.15)))
                    changed.append(f"world_bg strength={s.default_value:.3f}")

    print("[randomize] lighting: " + "; ".join(changed) if changed else
          "[randomize] no lights found")


def remove_buttons():
    """Delete the two yellow corner buttons from the scene."""
    for name in BUTTON_OBJECTS:
        obj = bpy.data.objects.get(name)
        if obj is None:
            print(f"[randomize] button '{name}' not found; skipping.")
            continue
        bpy.data.objects.remove(obj, do_unlink=True)
        print(f"[randomize] removed button: {name}")


def cleanup_previous_outputs():
    """Remove earlier generated scene_twin_*.blend outputs (and .blend1
    backups), keeping only the original scene."""
    for path in glob.glob(os.path.join(SCENE_DIR, "scene_twin_*.blend*")):
        if os.path.basename(path) == ORIGINAL_SCENE:
            continue
        try:
            os.remove(path)
            print(f"[randomize] removed old output: {os.path.basename(path)}")
        except OSError as e:
            print(f"[randomize] could not remove {path}: {e}")


def main():
    args = parse_args()
    tmp_dirs = []
    try:
        # First step: strip the two yellow corner buttons.
        remove_buttons()

        table_name = apply_set("table", args["table_dir"], args["table_forced"],
                               TABLE_OBJECT, TABLE_MATERIAL, tmp_dirs)
        # Optional: skipped when the bin/box material isn't in the scene (the
        # cryobox/vial task has no `cube_open` bin) or no box zips were given.
        box_name = apply_set("box", args["box_dir"], args["box_forced"],
                             BOX_OBJECT, BOX_MATERIAL, tmp_dirs,
                             optional=True)

        if args["randomize_table"]:
            randomize_table_size()
        else:
            print("[randomize] table resize disabled (pass --randomize-table to enable)")

        if args["randomize_light"]:
            randomize_lighting()
        else:
            print("[randomize] light randomization disabled")

        bpy.context.preferences.filepaths.save_version = 0  # no .blend1 backups
        cleanup_previous_outputs()

        # Strip '_' and '-' from each texture name, then keep the first 8 chars.
        def short(name):
            return name.replace("_", "").replace("-", "")[:8]

        # box_name is None when the box surface was skipped (not in this scene),
        # so leave it out of the filename rather than writing "box-None".
        stem = f"scene_twin_table-{short(table_name)}"
        if box_name:
            stem += f"_box-{short(box_name)}"
        out_path = os.path.join(SCENE_DIR, f"{stem}.blend")
        bpy.ops.wm.save_as_mainfile(filepath=out_path, copy=True)
        print(f"[randomize] Saved: {out_path}")
    finally:
        # Textures are packed into the .blend, so the extracted temp files
        # are intermediate and can go.
        for d in tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()

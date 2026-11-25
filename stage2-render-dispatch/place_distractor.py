"""
Place a random distractor object on the table in a Blender twin scene.

Runs headless inside Blender. Given the base twin scene and an episode
recording, this script:

  1. Builds a POOL of candidate points on the table surface that fall inside the
     context camera's field of view (Cam_Context).
  2. Filters that pool so every kept point is at least --clearance metres away
     from the footprint the blue cube (Blue_box) touches the table with during
     the episode (i.e. from all table-contact locations in the recording).
     It also avoids the robot base, basket, and buttons so the prop never spawns
     inside existing geometry.
  3. Picks a random distractor asset from the distractor/ directory (a Poly
     Haven <name>.blend.zip), appends its mesh, seats it on the tabletop at a
     random valid point, and parents it to the table so it rides the per-episode
     table-height shift applied by bake_animation.py.
  4. Saves the result to a distractor-prefixed .blend.

Usage:
  blender -b blender_scene/scene_twin.blend --python place_distractor.py -- \
      --episode episode_received_20260718_125608.json \
      --distractor-dir distractor \
      --output blender_scene/distractor_scene_twin.blend \
      [--asset croissant] [--clearance 0.05] [--seed 12345]

If --asset is omitted a random zip is chosen. If --seed is omitted the choice
and placement are nondeterministic.
"""

import os
import sys
import glob
import math
import json
import random
import zipfile
import tempfile

import bpy
from mathutils import Vector
from bpy_extras.object_utils import world_to_camera_view

# Unity primitive-cube half extent (metres) - the Blue_box footprint half-width.
BOX_HALF = 0.05
CONTEXT_CAMERA = "Cam_Context"
TABLE_NAME = "Table_1_Table 1"

# XY rectangles (in base-scene coords) of scene furniture the prop must avoid.
AVOID_RECTS = {
    "robot_base":   (0.18, 0.29, -0.04, 0.06),
    "basket":       (0.329, 0.529, 0.195, 0.395),
    "table_button": (0.465, 0.665, -0.052, 0.148),
    "cancel_button": (-0.266, -0.066, -0.052, 0.148),
}


# --------------------------------------------------------------------------- #
# args
# --------------------------------------------------------------------------- #
def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []

    def opt(*names, default=None):
        for name in names:
            if name in argv:
                return argv[argv.index(name) + 1]
        return default

    # Defaults resolve relative to this script's own directory (the repo root
    # for local runs, /opt/app in the container), mirroring randomize_textures.py.
    here = os.path.dirname(os.path.abspath(__file__))
    return {
        "episode": opt("--episode", default=os.path.join(
            here, "episode_received_20260718_125608.json")),
        "distractor_dir": opt("--distractor-dir", "--distractor_dir",
                              default=os.path.join(here, "distractor")),
        "output": opt("--output", default=os.path.join(
            here, "blender_scene", "distractor_scene_twin.blend")),
        "asset": opt("--asset", default=None),
        "clearance": float(opt("--clearance", default="0.05")),
        "seed": opt("--seed", default=None),
        "grid_step": float(opt("--grid-step", "--grid_step", default="0.02")),
        # Number of distractors to place. If omitted, a random count in
        # [1, --max-count] is chosen (default randomizes between 1 and 2).
        "count": opt("--count", default=None),
        "max_count": int(opt("--max-count", "--max_count", default="2")),
    }


# --------------------------------------------------------------------------- #
# geometry helpers
# --------------------------------------------------------------------------- #
def table_surface():
    """Return (top_z, (xmin, xmax, ymin, ymax)) of the table mesh in world space."""
    t = bpy.data.objects.get(TABLE_NAME)
    if t is None:
        raise RuntimeError("table object %r not found" % TABLE_NAME)
    corners = [t.matrix_world @ Vector(c) for c in t.bound_box]
    xs = [c.x for c in corners]
    ys = [c.y for c in corners]
    zs = [c.z for c in corners]
    return max(zs), (min(xs), max(xs), min(ys), max(ys))


def box_contact_footprint(episode_path, top_z_raised_tol=0.012):
    """List of (cx, cy) box centres (base-scene XY) where the cube rests on the
    table during the episode. Unity->Blender: X=unity.x, Y=unity.z, height=unity.y.

    A sample counts as 'contact' when the box bottom is within a small tolerance
    of the raised tabletop the episode was captured at.
    """
    ep = json.load(open(episode_path))
    offset = float(ep.get("tableHeightOffset", 0.0) or 0.0)
    top_z_base, _ = table_surface()
    top_raised = top_z_base + offset
    contacts = []
    for s in ep.get("samples", []):
        bp = s.get("boxPosition")
        if not bp:
            continue
        bx, by, bz = bp["x"], bp["z"], bp["y"]  # blender X, Y, height
        if (bz - BOX_HALF) - top_raised <= top_z_raised_tol:
            contacts.append((bx, by))
    return contacts


def pt_to_square_dist(px, py, cx, cy, half):
    """Distance from point (px,py) to the axis-aligned square centred (cx,cy)."""
    dx = max(abs(px - cx) - half, 0.0)
    dy = max(abs(py - cy) - half, 0.0)
    return math.hypot(dx, dy)


def in_rect(px, py, rect, margin=0.0):
    x0, x1, y0, y1 = rect
    return (x0 - margin) <= px <= (x1 + margin) and \
           (y0 - margin) <= py <= (y1 + margin)


def in_camera_fov(scene, cam, world_pt, inset=0.04):
    """True if world_pt projects inside the camera frame (with a small inset)."""
    co = world_to_camera_view(scene, cam, world_pt)
    return (inset <= co.x <= 1.0 - inset and
            inset <= co.y <= 1.0 - inset and co.z > 0.0)


# --------------------------------------------------------------------------- #
# distractor import
# --------------------------------------------------------------------------- #
def append_distractor(zip_path, tmp_dir):
    """Unzip a Poly Haven <name>.blend.zip and append all its objects.

    Returns (container_empty, mesh_objects). The mesh objects are parented to a
    new Empty named 'Distractor' so the whole prop can be transformed as a unit.
    """
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(tmp_dir)
    blends = glob.glob(os.path.join(tmp_dir, "**", "*.blend"), recursive=True)
    if not blends:
        raise RuntimeError("no .blend inside %s" % zip_path)
    blendpath = blends[0]

    before = set(bpy.data.objects)
    imgs_before = set(bpy.data.images)
    with bpy.data.libraries.load(blendpath, link=False) as (src, dst):
        dst.objects = list(src.objects)
    appended = [o for o in dst.objects if o is not None]

    # Pack the distractor's textures INTO the .blend. Poly Haven assets ship
    # their maps in a sibling textures/ folder referenced by relative paths; once
    # this scene is saved elsewhere (and rendered on the separate Deadline fleet,
    # where the extraction dir doesn't exist) those paths break and the material
    # renders as the pink "missing texture" placeholder. Packing embeds the
    # pixels so the scene is self-contained and portable.
    blend_dir = os.path.dirname(blendpath)
    packed = 0
    for img in (i for i in bpy.data.images if i not in imgs_before):
        try:
            abspath = os.path.normpath(bpy.path.abspath(img.filepath, start=blend_dir))
            if os.path.exists(abspath):
                img.filepath = abspath
                img.reload()
            img.pack()
            packed += 1
        except (RuntimeError, OSError) as exc:
            print("[distractor] WARNING: could not pack image %r: %s"
                  % (img.name, exc))
    print("[distractor] packed %d texture image(s) into the scene" % packed)

    coll = bpy.context.scene.collection
    for o in appended:
        if o.name not in coll.objects:
            coll.objects.link(o)
    bpy.context.view_layer.update()

    meshes = [o for o in appended if o.type == "MESH"]
    if not meshes:
        raise RuntimeError("distractor blend has no mesh objects: %s" % blendpath)

    empty = bpy.data.objects.new("Distractor", None)
    empty.empty_display_size = 0.05
    coll.objects.link(empty)
    bpy.context.view_layer.update()

    # Parent top-level appended objects to the empty (children follow).
    for o in appended:
        if o.parent is None or o.parent not in appended:
            o.parent = empty
            o.matrix_parent_inverse = empty.matrix_world.inverted()
    bpy.context.view_layer.update()
    return empty, meshes


def group_world_bbox(objs):
    xs, ys, zs = [], [], []
    for o in objs:
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            xs.append(w.x); ys.append(w.y); zs.append(w.z)
    return (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))


def pick_assets(distractor_dir, forced, count, rng):
    """Return `count` distractor zip paths. Distinct assets are preferred; if
    more are requested than exist, the list wraps (repeats allowed). A forced
    --asset (substring match) is always placed first."""
    zips = sorted(glob.glob(os.path.join(distractor_dir, "*.zip")))
    if not zips:
        raise RuntimeError("no .zip assets found in %s" % distractor_dir)
    order = zips[:]
    rng.shuffle(order)
    if forced:
        matches = [z for z in zips if forced.lower() in os.path.basename(z).lower()]
        if not matches:
            raise RuntimeError("no distractor zip matching %r (have: %s)"
                               % (forced, [os.path.basename(z) for z in zips]))
        first = matches[0]
        order = [first] + [z for z in order if z != first]
    chosen = []
    i = 0
    while len(chosen) < count:
        chosen.append(order[i % len(order)])
        i += 1
    return chosen


def build_candidates(scene, cam, top_z, bounds, foot_half, contacts,
                     clearance, step, placed):
    """Table-surface points inside the context-camera FOV that are >= clearance
    from the cube's contact footprint, off the furniture, and clear of any
    already-placed distractors in `placed` (list of (cx, cy, half))."""
    xmin, xmax, ymin, ymax = bounds
    edge = foot_half + 0.03            # keep the whole prop on the table
    avoid_margin = foot_half + 0.03
    valid = []
    x = xmin + edge
    while x <= xmax - edge:
        y = ymin + edge
        while y <= ymax - edge:
            ok = in_camera_fov(scene, cam, Vector((x, y, top_z)))
            if ok and contacts:
                dmin = min(pt_to_square_dist(x, y, cx, cy, BOX_HALF)
                           for (cx, cy) in contacts)
                ok = dmin >= clearance
            if ok and any(in_rect(x, y, r, avoid_margin) for r in AVOID_RECTS.values()):
                ok = False
            if ok:
                for (pcx, pcy, phalf) in placed:
                    if pt_to_square_dist(x, y, pcx, pcy, phalf) < foot_half + 0.03:
                        ok = False
                        break
            if ok:
                valid.append((x, y))
            y += step
        x += step
    return valid


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    args = parse_args()
    rng = random.Random(args["seed"]) if args["seed"] is not None else random.Random()

    scene = bpy.context.scene
    cam = bpy.data.objects.get(CONTEXT_CAMERA)
    if cam is None or cam.type != "CAMERA":
        raise RuntimeError("context camera %r not found" % CONTEXT_CAMERA)
    table = bpy.data.objects.get(TABLE_NAME)

    top_z, bounds = table_surface()
    xmin, xmax, ymin, ymax = bounds
    contacts = box_contact_footprint(args["episode"])
    clearance = args["clearance"]
    step = args["grid_step"]

    # How many distractors: explicit --count, else a random 1..max_count.
    if args["count"] is not None:
        count = max(1, int(args["count"]))
    else:
        count = rng.randint(1, max(1, args["max_count"]))

    print("[distractor] tabletop z=%.4f  table XY=[%.3f,%.3f]x[%.3f,%.3f]"
          % (top_z, xmin, xmax, ymin, ymax))
    print("[distractor] %d box table-contact samples; placing %d distractor(s)"
          % (len(contacts), count))

    assets = pick_assets(args["distractor_dir"], args["asset"], count, rng)
    placed = []          # (cx, cy, half) of distractors already seated
    n_placed = 0

    for idx, zip_path in enumerate(assets):
        print("[distractor] #%d asset: %s" % (idx + 1, os.path.basename(zip_path)))
        tmp_dir = tempfile.mkdtemp(prefix="distractor_")
        empty, meshes = append_distractor(zip_path, tmp_dir)

        x0, x1, y0, y1, z0, z1 = group_world_bbox(meshes)
        dcx, dcy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        foot_half = max(x1 - x0, y1 - y0) / 2.0
        print("[distractor] #%d dims: %.3f x %.3f x %.3f m (footprint half=%.3f)"
              % (idx + 1, x1 - x0, y1 - y0, z1 - z0, foot_half))

        valid = build_candidates(scene, cam, top_z, bounds, foot_half,
                                 contacts, clearance, step, placed)
        print("[distractor] #%d %d candidate points after all filters"
              % (idx + 1, len(valid)))
        if not valid:
            # First one must succeed; later ones are best-effort (place fewer).
            if n_placed == 0:
                raise RuntimeError("no valid placement found; loosen --clearance "
                                   "or asset too big")
            print("[distractor] #%d no room left; stopping at %d distractor(s)"
                  % (idx + 1, n_placed))
            # remove the just-appended (unplaced) prop
            for o in list(meshes) + [empty]:
                bpy.data.objects.remove(o, do_unlink=True)
            break

        px, py = rng.choice(valid)
        empty.location = (empty.location.x + (px - dcx),
                          empty.location.y + (py - dcy),
                          empty.location.z + (top_z - z0))
        bpy.context.view_layer.update()

        nx0, nx1, ny0, ny1, nz0, nz1 = group_world_bbox(meshes)
        ncx, ncy = (nx0 + nx1) / 2.0, (ny0 + ny1) / 2.0
        nhalf = max(nx1 - nx0, ny1 - ny0) / 2.0
        if contacts:
            final_clear = min(pt_to_square_dist(ncx, ncy, cx, cy, BOX_HALF)
                              for (cx, cy) in contacts)
        else:
            final_clear = float("inf")
        print("[distractor] #%d placed at XY=(%.4f, %.4f) bottom_z=%.4f; "
              "cube-footprint clearance=%.4f m (>= %.4f)"
              % (idx + 1, ncx, ncy, nz0, final_clear, clearance))

        # parent to the table so it rides the bake-time height shift
        empty.parent = table
        empty.matrix_parent_inverse = table.matrix_world.inverted()
        bpy.context.view_layer.update()

        placed.append((ncx, ncy, nhalf))
        n_placed += 1

    out = args["output"]
    os.makedirs(os.path.dirname(out), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=out)
    print("[distractor] placed %d distractor(s); saved -> %s" % (n_placed, out))


if __name__ == "__main__":
    main()

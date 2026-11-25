"""
Swap the scene's world background for a random HDRI environment (one episode =
one environment), and open the room up so the environment is actually visible.

Runs headless inside Blender. The twin's lab is a SEALED box (six solid panels
plus ceiling fixtures), so an
environment texture on the World is invisible from inside it and contributes no
light. Being an environment for the episode therefore means REPLACING the room
for the episode:

  1. Remove the room shell and its ceiling fixtures (panels, tubes, area
     lights). The table, task objects, robot and cameras are untouched.
  2. Load a random .exr/.hdr from --background-dir into a World environment
     texture at a random yaw, so the same HDRI frames differently per episode.
  3. Pack the image into the .blend (the render happens on the Deadline fleet,
     where the download directory does not exist).

Lighting then comes ENTIRELY from the HDRI, which is the point of an HDRI: the
hospital corridor lights the scene like a hospital corridor. The Sun-less,
fixture-less scene renders with believable image-based lighting for free.
--strength scales it if a particular set of HDRIs runs hot or cold.

Selection and yaw are seeded (--seed, e.g. the episode id) => reproducible.

Usage:
  blender -b scene.blend --python randomize_background.py -- \
      --background-dir /tmp/background --seed <episodeId> \
      [--background hospital] [--strength 1.0] [--output out.blend]

If --output is omitted the scene is saved in place.
"""

import glob
import hashlib
import math
import os
import random
import sys

import bpy

BG_EXTS = (".exr", ".hdr")

# The room shell object names expected in the base scene. Removed so the HDRI
# is visible.
ROOM_SHELL_NAMES = ("Floor", "Ceiling",
                    "Wall_East", "Wall_West", "Wall_North", "Wall_South")
FIXTURE_PREFIX = "Fluorescent_"


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []

    def opt(*names, default=None):
        for name in names:
            if name in argv:
                return argv[argv.index(name) + 1]
        return default

    here = os.path.dirname(os.path.abspath(__file__))
    return {
        "background_dir": opt("--background-dir", "--background_dir",
                              default=os.path.join(here, "room")),
        "background": opt("--background", default=None),  # substring force
        "seed": opt("--seed", default=None),
        # Index of this render among an episode's variants. Offsets the HDRI
        # choice so sibling variants get different environments (see pick_hdri).
        "variant": int(opt("--variant", default="0")),
        "strength": float(opt("--strength", default="1.0")),
        "output": opt("--output", default=None),
    }


def make_rng(seed):
    """Deterministic RNG from an arbitrary string (same scheme as
    randomize_camera.py); no seed -> system entropy."""
    if seed is None:
        return random.Random()
    digest = hashlib.sha256(str(seed).encode("utf-8")).hexdigest()
    return random.Random(int(digest, 16))


def pick_hdri(background_dir, forced, rng, variant=0):
    """Choose one HDRI, stratified across an episode's variants.

    `rng` (seeded by the episode id) picks where in the sorted list this EPISODE
    starts; `variant` then walks forward from there. So the N variants of one
    episode land on N DISTINCT environments whenever N <= len(found), while
    different episodes still start at different places.

    This needs no coordination between the variants' containers: each renders in
    isolation, derives the same sorted list from the same S3 prefix, and is told
    only its own index — so they reach non-colliding answers independently.
    Sampling every variant independently instead would collide often (with 3
    HDRIs and 3 variants, all-distinct only ~22% of the time).
    """
    found = []
    for ext in BG_EXTS:
        found += glob.glob(os.path.join(background_dir, "*" + ext))
    found = sorted(set(found))
    if not found:
        raise RuntimeError("no %s files in %s"
                           % ("/".join(BG_EXTS), background_dir))
    if forced:
        matches = [p for p in found
                   if forced.lower() in os.path.basename(p).lower()]
        if not matches:
            raise RuntimeError("no HDRI matching %r (have: %s)"
                               % (forced, [os.path.basename(p) for p in found]))
        return matches[0]
    # randrange draws exactly as rng.choice did, so variant 0 reproduces the
    # pre-stratification pick (and the yaw drawn from rng afterwards).
    return found[(rng.randrange(len(found)) + variant) % len(found)]


def remove_room():
    """Delete the sealed room shell and its fixtures so the HDRI shows.

    Also removes the fixtures' AREA lights: with an HDRI lighting the scene,
    leftover interior lights would double-light it (and the fixtures they
    belong to are gone). Everything else -- table, task objects, robot,
    cameras -- is left alone.
    """
    removed = []
    for o in list(bpy.data.objects):
        if o.name in ROOM_SHELL_NAMES or o.name.startswith(FIXTURE_PREFIX):
            removed.append(o.name)
            bpy.data.objects.remove(o, do_unlink=True)
    if removed:
        print("[background] removed room: %s" % ", ".join(sorted(removed)))
    else:
        print("[background] no room shell in this scene (already open)")


def set_world_hdri(path, yaw_rad, strength):
    """World = Environment Texture -> Background -> Output, rotated by yaw."""
    w = bpy.data.worlds.new("World_HDRI")
    w.use_nodes = True
    nt = w.node_tree
    nt.nodes.clear()

    out = nt.nodes.new("ShaderNodeOutputWorld")
    out.location = (400, 0)
    bg = nt.nodes.new("ShaderNodeBackground")
    bg.location = (200, 0)
    bg.inputs["Strength"].default_value = strength
    env = nt.nodes.new("ShaderNodeTexEnvironment")
    env.location = (-100, 0)
    env.image = bpy.data.images.load(path, check_existing=True)
    env.image.pack()  # self-contained: the fleet has no /tmp/background
    mapping = nt.nodes.new("ShaderNodeMapping")
    mapping.location = (-300, 0)
    mapping.inputs["Rotation"].default_value[2] = yaw_rad
    coord = nt.nodes.new("ShaderNodeTexCoord")
    coord.location = (-500, 0)

    nt.links.new(coord.outputs["Generated"], mapping.inputs["Vector"])
    nt.links.new(mapping.outputs["Vector"], env.inputs["Vector"])
    nt.links.new(env.outputs["Color"], bg.inputs["Color"])
    nt.links.new(bg.outputs["Background"], out.inputs["Surface"])

    for scene in bpy.data.scenes:
        scene.world = w
    return w


def main():
    args = parse_args()
    rng = make_rng(args["seed"])

    hdri = pick_hdri(args["background_dir"], args["background"], rng,
                     args["variant"])
    # Advance the stream once per variant so siblings also differ in yaw — which
    # matters when there are more variants than HDRIs and two of them wrap onto
    # the same file. Variant 0 advances zero times, so its yaw is unchanged.
    for _ in range(args["variant"]):
        rng.random()
    yaw = rng.uniform(0.0, 2.0 * math.pi)
    print("[background] selected %s, yaw %.1f deg, strength %.2f (variant %d)"
          % (os.path.basename(hdri), math.degrees(yaw), args["strength"],
             args["variant"]))

    remove_room()
    set_world_hdri(hdri, yaw, args["strength"])

    out = args["output"]
    if out:
        bpy.ops.wm.save_as_mainfile(filepath=out)
        print("[background] saved -> %s" % out)
    else:
        bpy.ops.wm.save_mainfile()
        print("[background] saved in place")


if __name__ == "__main__":
    main()

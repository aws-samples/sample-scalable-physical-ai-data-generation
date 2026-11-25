# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Bake an episode recording onto an ALREADY-BUILT Blender twin scene, without
rebuilding any geometry.

  blender --background --python bake_animation.py -- \
      <blender_twin_dir> <episode.json> [target.blend] [output.blend]

Defaults:
  target.blend  -> out/scene_twin.blend
  output.blend  -> overwrites target
  rig anchors   -> out/rig.json (produced by export_scene.py)

The target scene must already contain:

  * the articulated rig -- Empties named base_link, shoulder_link ...
    moving_jaw_so101_v1_link
  * an object per manipulated thing, named exactly as Unity named it: CryoBox,
    CryoBoxLid, VialRack, Vial_11 ... and BoxVial_00 ...

i.e. a scene built by build_twin.py. (It used to say "and the box object
Blue_box"; that object went with the pick-and-place task.)

IMPORTANT: the object set depends on the episode. How many stored vials exist is
decided at runtime from how many wells were left free, so a scene built from one
episode may not have the objects another episode needs. Anything absent is listed
as missing and simply not animated -- rebuild with build_twin.py if that happens.

Requires rig.json (out/rig.json) for the joint anchor frames. If you only have
the .blend and no rig.json, re-run: python3 export_scene.py ..
"""
import bpy
import os
import sys
import json

argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []
if len(argv) < 2:
    print('usage: -- <blender_twin_dir> <episode.json> [target.blend] [output.blend]')
    sys.exit(1)

BASE = argv[0]
EPISODE = argv[1]
OUT = os.path.join(BASE, 'out')
TARGET = argv[2] if len(argv) > 2 else os.path.join(OUT, 'scene_twin.blend')
OUTPUT = argv[3] if len(argv) > 3 else TARGET

sys.path.insert(0, BASE)
import episode_anim as EA

# Same rate build_twin.py bakes at. These two used to disagree by omission:
# build_twin passed fps=30 explicitly and this file passed nothing at all.
FPS = EA.DEFAULT_FPS


def main():
    rig_path = os.path.join(OUT, 'rig.json')
    if not os.path.exists(rig_path):
        print('ERROR: %s not found - run: python3 export_scene.py ..' % rig_path)
        sys.exit(1)
    if not os.path.exists(TARGET):
        print('ERROR: target blend not found: %s' % TARGET)
        sys.exit(1)

    rig = json.load(open(rig_path))
    bpy.ops.wm.open_mainfile(filepath=TARGET)

    # Own the table height: shift the table group to the height THIS episode was
    # captured at before baking. Idempotent via a scene property, so it applies
    # only the delta vs. whatever height the opened scene already carries (from
    # build_twin or a previous bake) and never stacks offsets.
    prev = EA.current_table_height()
    EA.set_table_height(EA.resolve_table_offset(EPISODE))
    print('table height: was %.4f m in opened scene' % prev)

    last, missing, report = EA.apply_episode(rig, bpy.data.objects.get, EPISODE,
                                            fps=FPS)
    if missing:
        print('WARNING: %d object(s) in the episode are not in this scene, so they '
              'were not animated:' % len(missing))
        for name in missing:
            print('   - %s' % name)
        print('   The object set is per-episode (the number of stored vials '
              'depends on how many wells were free). Rebuild for THIS episode:')
        print('     blender -b --python build_twin.py -- %s %s'
              % (BASE, EPISODE))
    print('baked %d frames from %s at %d fps' % (last, os.path.basename(EPISODE), FPS))
    EA.print_report(report, os.path.basename(EPISODE))

    bpy.ops.wm.save_as_mainfile(filepath=OUTPUT)
    print('saved %s' % OUTPUT)


main()

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Shared episode->animation logic for the Blender twin (runs inside Blender).

Keyframes an existing articulated SO-101 rig (Empties named by joint link) and
the manipulated box from an episode recording, using Unity's ArticulationBody
revolute model:  childLocal(angle) = A_parent . Rot_x(angle) . A_child^-1.

Used by both build_twin.py (fresh build) and bake_animation.py (existing .blend).
"""
import json
import math
from mathutils import Matrix, Quaternion

# Unity->Blender basis change (swap Y/Z); C == C^-1.
C = Matrix(((1, 0, 0, 0), (0, 0, 1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))


def m16(lst):
    return Matrix((lst[0:4], lst[4:8], lst[8:12], lst[12:16]))


def joint_local_blender(link, deg):
    """Blender local matrix (relative to parent link) at drive angle deg."""
    A_p = m16(link['A_p'])
    A_c = m16(link['A_c'])
    Rx = Matrix.Rotation(math.radians(deg), 4, 'X')
    return C @ (A_p @ Rx @ A_c.inverted()) @ C


def u2b_pose(pos, quat):
    """Unity world pos{x,y,z}+quat{x,y,z,w} -> Blender world matrix (via C)."""
    T = Matrix.Translation((pos['x'], pos['y'], pos['z']))
    R = Quaternion((quat['w'], quat['x'], quat['y'], quat['z'])).to_matrix().to_4x4()
    return C @ (T @ R) @ C


def episode_field(joint_key):
    # the base joint's field is "base_angle" in the recordings
    return 'base_angle' if joint_key == 'base' else joint_key


def _fcurves(ob):
    ad = ob.animation_data
    if not ad or not ad.action:
        return []
    act = ad.action
    if hasattr(act, 'fcurves') and len(act.fcurves):
        return act.fcurves
    out = []
    try:
        for layer in act.layers:
            for strip in layer.strips:
                for cbag in strip.channelbags:
                    out.extend(cbag.fcurves)
    except Exception:
        pass
    return out


# Recorded object kinds the pose tracks must NOT keyframe, and who owns them:
#
#   arm_link  the rig, from joint angles through rig.json's hinge matrices. The
#             measured link poses stay in the file as the ground truth that model
#             is checked against -- see check_arm_fk.
#   table     the FBX table import, moved as a group by set_table_height. The
#             recorded object is also named differently in Blender ('Table_1_...'),
#             so keying it would only add a phantom entry to `missing`.
#   button    not in the twin at all. The Restart button is a capture control, so
#             export_scene's EXCLUDE_NAMES drops it; the recording still carries
#             its pose, and keying it would resurrect it as an empty.
#
# Everything else -- the cryobox, its lid, all twenty vials, the rack -- is owned
# by the recording, because it is the only thing that knows where they were.
NOT_KEYED_FROM_POSES = {'arm_link', 'table', 'button'}

# Bake frame rate, shared so build_twin and bake_animation cannot disagree.
# Recording runs at ~10 Hz and this is 30, so keyframes land every third frame and
# Blender interpolates linearly between them. It is NOT derived from the episode:
# changing it would silently rescale time in an already-built scene, since
# `frame = 1 + round(t * fps)`.
DEFAULT_FPS = 30


def apply_episode(rig, get_object, episode_path, fps=DEFAULT_FPS, clear=True):
    """Keyframe the rig + every recorded object from an episode file.

    rig        : dict from rig.json ({'links':[...], 'box':{...}})
    get_object : callable(name) -> bpy object or None (e.g. bpy.data.objects.get)
    Returns (last_frame, missing_names, report).

    TWO SOURCES OF TRUTH, BY DESIGN

    The arm is keyed from joint angles through rig.json's A_p/A_c hinge matrices,
    as it always was. Everything else is keyed from the episode's own object list:
    schema 2 records `objects[]` alongside a `poses[]` per sample, one entry per
    object, in the same order. That is how the cryobox, its lid and all twenty
    vials get placed -- none of which the old fixed-name path could express.

    An object is matched to the scene by name, and anything unmatched is returned
    in `missing` rather than skipped quietly. A Unity name with no Blender
    counterpart means the twin is missing a mesh, and that is worth a line in the
    build log, not a shrug.
    """
    import bpy
    ep = json.load(open(episode_path))
    samples = ep.get('samples', [])
    if not samples:
        return 0, [], None

    joints = [l for l in rig['links'] if not l['fixed'] and l['joint_key']]
    box_name = (rig.get('box') or {}).get('name')

    # resolve objects, clear existing animation
    objs = {}
    missing = []
    for link in joints:
        o = get_object(link['name'])
        if o is None:
            missing.append(link['name'])
        else:
            objs[link['name']] = o
            if clear:
                o.animation_data_clear()
            o.rotation_mode = 'XYZ'
    box_ob = get_object(box_name) if box_name else None
    if box_ob is not None:
        if clear:
            box_ob.animation_data_clear()
        box_ob.rotation_mode = 'QUATERNION'
    elif box_name:
        missing.append(box_name)

    # Schema 2: the recorded object list. `tracks` is parallel to `poses` in every
    # sample, with None where the scene has no object of that name.
    #
    # NOT_KEYED_FROM_POSES is about ownership. Every object in the twin must have
    # exactly one thing deciding where it is, or whichever writes last wins and the
    # result depends on call order.
    ep_objects = ep.get('objects') or []
    tracks = []
    for od in ep_objects:
        name = od.get('id')
        if od.get('kind') in NOT_KEYED_FROM_POSES or not name:
            tracks.append(None)
            continue
        o = get_object(name)
        if o is None:
            missing.append(name)
            tracks.append(None)
            continue
        # The same object reached by both paths: leave it to the rig.
        if o is box_ob or name in objs:
            tracks.append(None)
            continue
        if clear:
            o.animation_data_clear()
        o.rotation_mode = 'QUATERNION'
        tracks.append(o)

    # keyframe rotation as quaternions to avoid Euler wrap/gimbal jumps;
    # enforce hemisphere continuity (negate q if it flipped sign vs previous)
    # so linear interpolation always takes the short way.
    for link in joints:
        o = objs.get(link['name'])
        if o is not None:
            o.rotation_mode = 'QUATERNION'
    prev_q = {}

    def keyed_quat(o, M, frame):
        loc = M.to_translation()
        q = M.to_quaternion()
        pq = prev_q.get(o.name)
        if pq is not None and q.dot(pq) < 0.0:
            q.negate()
        prev_q[o.name] = q
        o.location = loc
        o.rotation_quaternion = q
        o.keyframe_insert('location', frame=frame)
        o.keyframe_insert('rotation_quaternion', frame=frame)

    sc = bpy.context.scene
    sc.render.fps = fps
    last_frame = 1
    for s in samples:
        frame = 1 + int(round(s.get('t', 0.0) * fps))
        last_frame = max(last_frame, frame)
        sc.frame_set(frame)
        for link in joints:
            o = objs.get(link['name'])
            if o is None:
                continue
            ang = s.get(episode_field(link['joint_key']))
            if ang is None:
                continue
            keyed_quat(o, joint_local_blender(link, float(ang)), frame)
        if box_ob is not None and 'boxPosition' in s and 'boxRotation' in s:
            keyed_quat(box_ob, u2b_pose(s['boxPosition'], s['boxRotation']), frame)

        poses = s.get('poses') or []
        for i, o in enumerate(tracks):
            if o is None or i >= len(poses):
                continue
            p = poses[i]
            keyed_quat(o, u2b_pose(p['position'], p['rotation']), frame)

    sc.frame_start = 1
    sc.frame_end = last_frame
    sc.frame_set(1)
    keyed = list(objs.values()) + ([box_ob] if box_ob else []) \
        + [o for o in tracks if o is not None]
    for o in keyed:
        for fc in _fcurves(o):
            for kp in fc.keyframe_points:
                kp.interpolation = 'LINEAR'

    # The arm-leads-carried-object check needs something the gripper actually
    # carries. That used to be the one animated cube; in this task it is a vial,
    # so take the first payload vial the recording lists and only fall back to the
    # rig's box for pre-v2 files.
    carried = next((o.get('id') for o in ep_objects
                    if o.get('role') == 'payload' and o.get('id')), None) or box_name
    report = diagnose_sync(get_object, 1, last_frame, fps=fps,
                           box_name=carried) if carried else None
    if report is None:
        report = {'assessable': False,
                  'reason': 'no gripper/carried-object pair to correlate'}
    # Always run, even when the correlation check declined: this one only needs
    # the recorded arm-link poses, and it is the stronger of the two.
    report['arm_fk'] = check_arm_fk(get_object, ep, fps=fps)
    return last_frame, missing, report


def check_arm_fk(get_object, ep, fps=DEFAULT_FPS, tol=0.002, probes=12):
    """Does the hinge model put the arm where Unity measured it?

    The arm is keyed from joint angles through rig.json's A_p/A_c matrices, which
    is a model. Schema 2 also carries the world pose each arm link actually had,
    which is a measurement. Comparing the two is the only way to catch a wrong
    hinge axis, a stale rig.json or a degrees/radians slip -- all of which produce
    a scene that looks fine until you notice the gripper is not where the vial is.

    Deliberately checks the BAKED scene against the RECORDED poses, not the joint
    angles against themselves: a check that re-derives its expectation from the
    thing under test agrees with itself while both sides are wrong.

    Returns None when the episode predates schema 2 (nothing to compare against).
    """
    import bpy
    objects = ep.get('objects') or []
    samples = ep.get('samples') or []
    links = [(i, od.get('id')) for i, od in enumerate(objects)
             if od.get('kind') == 'arm_link' and od.get('id')]
    if not links or not samples:
        return None

    resolved = [(i, get_object(name), name) for i, name in links]
    resolved = [(i, o, n) for i, o, n in resolved if o is not None]
    if not resolved:
        return {'assessable': False, 'reason': 'no arm links in the scene'}

    # A frame_set per probe is not free, so sample the episode rather than walk it.
    step = max(1, len(samples) // max(1, probes))
    sc = bpy.context.scene
    worst, worst_link, checked = 0.0, None, 0
    for s in samples[::step]:
        poses = s.get('poses') or []
        if not poses:
            continue
        sc.frame_set(1 + int(round(s.get('t', 0.0) * fps)))
        for i, o, name in resolved:
            if i >= len(poses):
                continue
            p = poses[i]
            measured = u2b_pose(p['position'], p['rotation']).to_translation()
            d = (o.matrix_world.translation - measured).length
            checked += 1
            if d > worst:
                worst, worst_link = d, name
    sc.frame_set(1)
    if not checked:
        return {'assessable': False, 'reason': 'no poses to compare'}
    return {'assessable': True, 'checked': checked, 'worst_m': worst,
            'worst_link': worst_link, 'ok': worst <= tol, 'tol_m': tol}


def print_report(report, episode_name=''):
    if not report:
        return
    fk = report.get('arm_fk')
    if fk and fk.get('assessable'):
        if fk['ok']:
            print('[fk check] OK: rig matches the measured arm to %.1f mm '
                  '(%d probes).' % (fk['worst_m'] * 1000.0, fk['checked']))
        else:
            print('=' * 68)
            print('[fk check] WARNING: the rigged arm is up to %.1f mm from where '
                  'Unity measured it' % (fk['worst_m'] * 1000.0))
            print('  worst link: %s (tolerance %.1f mm, %d probes)'
                  % (fk['worst_link'], fk['tol_m'] * 1000.0, fk['checked']))
            print('  => rig.json hinge data and the recording disagree. Re-export '
                  'the rig (robot_rig.py) before trusting these renders.')
            print('=' * 68)
    elif fk:
        print('[fk check] not assessable (%s)' % fk.get('reason', '?'))
    if not report.get('assessable'):
        print('[sync check] not assessable (%s)' % report.get('reason', '?'))
        return
    if report.get('commanded'):
        print('=' * 68)
        print('[sync check] WARNING: arm LEADS the box by %d frames (%.0f ms).'
              % (report['lead_frames'], report['lead_ms']))
        print('  This is the commanded-angle signature: the recording %s likely'
              % (episode_name or 'used'))
        print('  stored COMMANDED joint targets, not ACTUAL joint angles.')
        print('  => Recompile the Unity scripts (EpisodeRecorder / '
              'UnityRobotController) and RE-RECORD; the box will then track the '
              'gripper.')
        print('=' * 68)
    else:
        print('[sync check] OK: arm and box are in sync (lead %d frames).'
              % report['lead_frames'])


def diagnose_sync(get_object, frame_start, frame_end, fps=DEFAULT_FPS,
                  gripper_name='moving_jaw_so101_v1_link', box_name=None):
    """Detect whether the recorded arm LEADS the carried object.

    Cross-correlates gripper vs carried-object velocity over the frames where the
    object is actually moving (grasp/carry). If its velocity best matches an
    EARLIER gripper velocity, the arm leads it -> the episode likely stored
    commanded joint targets instead of actual joint angles.

    `box_name` has no default: it used to be 'Blue_box', which quietly stopped
    existing, so this returned None on every episode and the check never ran at
    all. apply_episode passes the first payload vial -- whatever the gripper
    actually carries in this task.

    Returns a dict, or None if it can't assess.
    """
    import bpy
    sc = bpy.context.scene
    jaw = get_object(gripper_name)
    box = get_object(box_name) if box_name else None
    if jaw is None or box is None:
        return None

    G, B = [], []
    for f in range(frame_start, frame_end + 1):
        sc.frame_set(f)
        G.append(jaw.matrix_world.translation.copy())
        B.append(box.matrix_world.translation.copy())
    sc.frame_set(frame_start)
    if len(G) < 6:
        return None

    gv = [G[i + 1] - G[i] for i in range(len(G) - 1)]
    bv = [B[i + 1] - B[i] for i in range(len(B) - 1)]
    speeds = [v.length for v in bv]
    peak = max(speeds) if speeds else 0.0
    if peak < 1e-4:
        return {'assessable': False, 'reason': 'box never moves'}
    thr = peak * 0.15
    moving = [i for i, s in enumerate(speeds) if s > thr]
    if len(moving) < 5:
        return {'assessable': False, 'reason': 'too little box motion'}
    lo, hi = min(moving), max(moving)

    best_L, best_c = 0, None
    for L in range(-8, 9):
        num = cnt = 0.0
        for i in range(lo, hi + 1):
            j = i + L
            if 0 <= j < len(gv):
                num += gv[j].dot(bv[i]); cnt += 1
        if cnt:
            c = num / cnt
            if best_c is None or c > best_c:
                best_c, best_L = c, L
    # bv[i] ~= gv[i-d] when arm leads by d frames -> best correlation at L=-d
    lead = -best_L
    return {'assessable': True, 'lead_frames': lead,
            'lead_ms': 1000.0 * lead / fps,
            'commanded': lead >= 2}


# ---------------------------------------------------------------------------
# Table height (standing-desk offset)
#
# The episode's `tableHeightOffset` is the height the workspace was raised to at
# capture. The table group (table, robot base, container, buttons) is shifted by
# it; the box is NOT shifted because its recorded world pose already lives in the
# raised world.
#
# `set_table_height` is IDEMPOTENT: it records the applied offset on the scene
# (`scene['table_height_offset']`) and only shifts by the delta vs. whatever is
# already applied. This lets any step (build_twin OR bake_animation) set the
# height for the episode it is baking without stacking offsets across runs.
# ---------------------------------------------------------------------------
# What rides the standing desk. Only things whose pose is NOT recorded belong
# here: the cryobox, the vials and the rack are keyframed from the episode in
# absolute world space, so shifting them as well would move them twice.
#
# 'cube_open', 'CancelButton' and 'TableButton' used to be in this set and none of
# them reach the twin any more -- the open-topped bin went with the pick-and-place
# task, the Cancel button was folded into Restart, and Restart itself is now
# excluded at export as a capture control. Dead names are harmless but they imply
# the set is maintained, so they are gone.
#
# 'Cam_Context' is here because it FRAMES the table: it is aimed at the workspace
# from a fixed station in front of the table, so a raised desk that left the
# camera behind would re-frame the shot. It rides the shift rather than being
# parented to the table, because stage2's randomize_camera.py writes cam.location
# in the camera's own parent space and would be thrown off by a parent.
TABLE_GROUP_EXACT = {'base_link', 'Cam_Context'}
TABLE_GROUP_PREFIX = ('Table_1', 'Table 1', 'Table_')
_TABLE_HEIGHT_PROP = 'table_height_offset'
_BOX_HALF_HEIGHT = 0.05  # Unity primitive cube box half-height (metres)


def _is_table_group(name):
    return name in TABLE_GROUP_EXACT or name.startswith(TABLE_GROUP_PREFIX)


def table_top_z():
    """Max world Z of the table meshes in the current scene, or None."""
    import bpy
    zmax = None
    for o in bpy.data.objects:
        if o.type == 'MESH' and o.name.startswith(TABLE_GROUP_PREFIX):
            for v in o.data.vertices:
                z = (o.matrix_world @ v.co).z
                zmax = z if zmax is None else max(zmax, z)
    return zmax


def current_table_height():
    """The table-height offset currently applied to the scene (0 if none)."""
    import bpy
    return float(bpy.context.scene.get(_TABLE_HEIGHT_PROP, 0.0) or 0.0)


def set_table_height(offset):
    """Set the table group to `offset` metres above design height (Blender +Z).

    Idempotent: shifts only by the delta vs. the offset already recorded on the
    scene, then records the new offset. Only root objects in the table group are
    moved (children follow). Returns the delta actually applied.
    """
    import bpy
    from mathutils import Matrix
    sc = bpy.context.scene
    current = current_table_height()
    delta = float(offset) - current
    if abs(delta) >= 1e-4:
        for o in bpy.data.objects:
            if o.parent is None and _is_table_group(o.name):
                o.matrix_world = Matrix.Translation((0, 0, delta)) @ o.matrix_world
        print('table height: set to %.4f m (delta %+.4f m applied to table group)'
              % (offset, delta))
    else:
        print('table height: already at %.4f m (no change)' % offset)
    sc[_TABLE_HEIGHT_PROP] = float(offset)
    return delta


def resolve_table_offset(episode_path):
    """dy from the episode's `tableHeightOffset` field.

    THE FIELD IS AUTHORITATIVE WHENEVER IT IS PRESENT, INCLUDING 0.0. Capturing
    at the design height is the normal case, not a missing value, and conflating
    the two is what dropped the table (and the robot bolted to it) 5 cm below the
    cryobox, the rack and the vials: the fallback below read `boxPosition`, which
    in schema 2 is the CRYOBOX -- whose recorded origin already sits ON the
    tabletop, not 5 cm above it like the old Unity-primitive cube the
    `_BOX_HALF_HEIGHT` subtraction was written for. So it "inferred" a -0.05 m
    desk offset out of a perfectly level scene.

    The inference is also state-dependent, which no resolver should be: it
    measures the table in the scene as currently loaded, so re-baking an
    already-shifted twin computed a *different* answer and silently undid the
    error. Kept only for pre-schema-2 recordings, which genuinely lack the field
    and whose `boxPosition` really was the primitive cube.
    """
    ep = json.load(open(episode_path))
    if 'tableHeightOffset' in ep:
        dy = float(ep.get('tableHeightOffset') or 0.0)
        print('table height offset from episode: %.3f m' % dy)
        return dy
    if int(ep.get('schemaVersion', 0) or 0) >= 2:
        print('table height: schema 2 episode with no tableHeightOffset field; '
              'leaving the table at design height')
        return 0.0

    samples = ep.get('samples')
    if not samples:
        return 0.0
    top = table_top_z()
    if top is None:
        return 0.0
    # Legacy inference, pre-schema-2 only, and it is allowed to fail. This used to
    # index samples[0]['boxPosition']['y'] outright, which raised KeyError the
    # moment the manipulated object stopped being the old Blue_box.
    bp = (samples[0].get('boxPosition') or {})
    if 'y' not in bp:
        print('table height: episode has no tableHeightOffset and no box pose to '
              'infer one from; leaving the table at design height')
        return 0.0
    box_bottom = bp['y'] - _BOX_HALF_HEIGHT  # blender z = unity y
    dy = box_bottom - top
    if abs(dy) >= 1e-4:
        print('table height offset inferred from box rest: %.3f m '
              '(pre-schema-2 episode has no tableHeightOffset field)' % dy)
    return dy

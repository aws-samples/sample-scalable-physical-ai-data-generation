# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bake render/quality/color-management settings into a .blend, in place-ish.

Runs INSIDE Blender (headless). The AWS Deadline Cloud Blender *adaptor* renders
a scene using the settings saved in the .blend -- it does NOT run our render-time
apply_overrides.py. So when submitting through the adaptor, we first bake the
desired settings into a prepared copy of the scene with this script, then submit
that copy.

Usage:
    blender -b input.blend --python prepare_scene.py -- '<settings-json>' output.blend

<settings-json> keys (all optional; omit to leave the scene's value):
    engine            "CYCLES" | "EEVEE" | "SCENE"
    res_percent       int 1..100
    res_x, res_y      int (0 = keep)
    samples           int (0 = keep)
    noise_threshold   float (0 disables adaptive sampling)
    light_threshold   float
    denoise           "ON" | "OFF" | "SCENE"
    exposure          float (color management, stops)
    gamma             float
    view_transform    str  | "SCENE"
    look              str  | "SCENE"
"""
import bpy
import json
import sys


def get_argv_after_ddash():
    if "--" in sys.argv:
        return sys.argv[sys.argv.index("--") + 1:]
    return []


def try_set(obj, attr, value):
    try:
        setattr(obj, attr, value)
    except (TypeError, AttributeError, ValueError):
        print(f"[prepare_scene] skipped {attr}={value!r}")


def set_engine(scene, name):
    if name == "CYCLES":
        candidates = ["CYCLES"]
    else:
        candidates = ["BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"]
    for cand in candidates:
        try:
            scene.render.engine = cand
            return
        except TypeError:
            continue
    print(f"[prepare_scene] WARNING: could not set engine {name}")


def apply(scene, s):
    engine = s.get("engine", "SCENE")
    if engine != "SCENE":
        set_engine(scene, engine)

    res_x = int(s.get("res_x", 0))
    res_y = int(s.get("res_y", 0))
    if res_x > 0 and res_y > 0:
        scene.render.resolution_x = res_x
        scene.render.resolution_y = res_y
    if "res_percent" in s:
        scene.render.resolution_percentage = int(s["res_percent"])

    eng = scene.render.engine
    samples = int(s.get("samples", 0))
    if samples > 0:
        if eng == "CYCLES":
            scene.cycles.samples = samples
        else:
            scene.eevee.taa_render_samples = samples

    if eng == "CYCLES":
        cy = scene.cycles
        nthr = float(s.get("noise_threshold", -1))
        if nthr > 0.0:
            try_set(cy, "use_adaptive_sampling", True)
            try_set(cy, "adaptive_threshold", nthr)
        elif nthr == 0.0:
            try_set(cy, "use_adaptive_sampling", False)
        if "light_threshold" in s:
            try_set(cy, "use_light_tree", True)
            try_set(cy, "light_sampling_threshold", float(s["light_threshold"]))
        # render on GPU (the adaptor also sets gpu_device, this is belt-and-braces)
        try_set(cy, "device", "GPU")

    denoise = s.get("denoise", "SCENE")
    if denoise != "SCENE" and eng == "CYCLES":
        cy = scene.cycles
        cy.use_denoising = (denoise == "ON")
        if denoise == "ON":
            try_set(cy, "denoiser", "OPENIMAGEDENOISE")
            try_set(cy, "denoising_input_passes", "RGB_ALBEDO")
            try_set(cy, "denoising_prefilter", "FAST")
            try_set(cy, "denoising_quality", "BALANCED")
            try_set(cy, "denoising_use_gpu", True)

    # Color management (engine-independent)
    ds = scene.display_settings
    vs = scene.view_settings
    try_set(ds, "display_device", "sRGB")
    if s.get("view_transform", "SCENE") != "SCENE":
        try_set(vs, "view_transform", s["view_transform"])   # before look
    if s.get("look", "SCENE") != "SCENE":
        try_set(vs, "look", s["look"])
    if "gamma" in s:
        try_set(vs, "gamma", float(s["gamma"]))
    if "exposure" in s:
        vs.exposure = float(s["exposure"])

    # Fixed Cycles quality (match the laptop scene)
    if eng == "CYCLES":
        cy = scene.cycles
        try_set(cy, "film_exposure", 1.0)
        try_set(cy, "pixel_filter_type", "BLACKMAN_HARRIS")
        try_set(cy, "filter_width", 1.5)
        try_set(cy, "max_bounces", 12)
        try_set(cy, "diffuse_bounces", 4)
        try_set(cy, "glossy_bounces", 4)
        try_set(cy, "transmission_bounces", 12)
        try_set(cy, "volume_bounces", 0)
        try_set(cy, "transparent_max_bounces", 8)


def main():
    argv = get_argv_after_ddash()
    if len(argv) < 2:
        sys.stderr.write("usage: ... -- '<settings-json>' <output.blend>\n")
        raise SystemExit(2)
    settings = json.loads(argv[0])
    out_path = argv[1]

    for scene in bpy.data.scenes:
        apply(scene, settings)

    bpy.ops.wm.save_as_mainfile(filepath=out_path)
    print(f"[prepare_scene] saved prepared scene -> {out_path}")


if __name__ == "__main__":
    main()

# Blender reconstruction + submit image.
#
# Runs the reconstruction (blender_twin/bake_animation.py) headless, bakes the
# render/quality settings (prepare_scene.py), then submits the prepared scene to
# an existing AWS Deadline Cloud farm/queue via submit_to_deadline.py.
#
# This image does NOT render — rendering happens on the Deadline fleet. So it is
# CPU-only and needs no GPU.
#
# Build context is the stage2-render-dispatch/ root (see BlenderImage in
# lib/render-dispatch-stack.ts); .dockerignore there limits it to the files below.
FROM public.ecr.aws/docker/library/python:3.11-slim

# Blender release (LTS). Override at build time with --build-arg if needed.
# Must be a patch that still exists on the download server/mirrors (4.5.0 was
# pruned; 4.5.3 is a published 4.5 LTS maintenance release). Any 4.5.x can open
# a scene_twin.blend built with a 4.5 local Blender.
ARG BLENDER_VERSION=4.5.3
ARG BLENDER_SERIES=4.5

ENV DEBIAN_FRONTEND=noninteractive \
    BLENDER_DIR=/opt/blender \
    APP_DIR=/opt/app \
    PATH=/opt/blender:$PATH

# Runtime libraries Blender needs even in headless (--background) mode.
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget xz-utils ca-certificates \
        libgl1 libegl1 libsm6 libice6 libxrender1 libxxf86vm1 \
        libxfixes3 libxi6 libxkbcommon0 libxext6 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Install Blender (portable tarball) and expose the executable as `blender`.
# Try the official server first, then public mirrors, with retries — the
# official CDN sometimes 403/503s automated clients (wget exit 8).
RUN set -eux; \
    fname="blender-${BLENDER_VERSION}-linux-x64.tar.xz"; \
    urls="\
https://download.blender.org/release/Blender${BLENDER_SERIES}/${fname} \
https://mirrors.ocf.berkeley.edu/blender/release/Blender${BLENDER_SERIES}/${fname} \
https://mirror.clarkson.edu/blender/release/Blender${BLENDER_SERIES}/${fname} \
https://ftp.halifax.rwth-aachen.de/blender/release/Blender${BLENDER_SERIES}/${fname}"; \
    ok=0; \
    for u in $urls; do \
        echo "trying $u"; \
        if wget -q --tries=3 --timeout=60 "$u" -O /tmp/blender.tar.xz; then ok=1; break; fi; \
    done; \
    [ "$ok" = 1 ] || { echo "ERROR: all Blender mirrors failed"; exit 1; }; \
    mkdir -p "$BLENDER_DIR"; \
    tar -xf /tmp/blender.tar.xz -C "$BLENDER_DIR" --strip-components=1; \
    rm /tmp/blender.tar.xz; \
    ln -s "$BLENDER_DIR/blender" /usr/local/bin/blender

# Python deps for the entrypoint (S3/DDB/SSM) and Deadline submission.
RUN pip install --no-cache-dir boto3 "deadline[default]"

WORKDIR ${APP_DIR}
# Everything lands flat in APP_DIR: run.py invokes the scripts by
# APP_DIR/<name>.py and submit_to_deadline.py expects ./deadline_bundle.
COPY blender_twin/ ${APP_DIR}/
COPY submit_to_deadline.py randomize_textures.py randomize_camera.py \
     randomize_background.py place_distractor.py rotate_bin.py ${APP_DIR}/
COPY docker/blender/run.py ${APP_DIR}/run.py

ENTRYPOINT ["python3", "/opt/app/run.py"]

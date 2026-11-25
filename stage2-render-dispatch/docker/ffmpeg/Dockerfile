# Video-encode image.
#
# Downloads the rendered frames from a completed Deadline Cloud job, stitches
# them into an MP4 with ffmpeg, and uploads the result to the output bucket.
# Small image: ffmpeg + the deadline CLI + boto3. No Blender.
FROM public.ecr.aws/docker/library/python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    APP_DIR=/opt/app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir boto3 "deadline[default]"

WORKDIR ${APP_DIR}
COPY run.py ${APP_DIR}/run.py

ENTRYPOINT ["python3", "/opt/app/run.py"]

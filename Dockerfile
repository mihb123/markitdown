FROM python:3.13-slim-trixie

ENV DEBIAN_FRONTEND=noninteractive
ENV EXIFTOOL_PATH=/usr/bin/exiftool
ENV FFMPEG_PATH=/usr/bin/ffmpeg
ENV ORT_DISABLE_TELEMETRY=1

# Runtime dependency
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libimage-exiftool-perl

ARG INSTALL_GIT=false
RUN if [ "$INSTALL_GIT" = "true" ]; then \
    apt-get install -y --no-install-recommends \
    git; \
    fi

# Cleanup
RUN rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
RUN pip --no-cache-dir install \
    /app/packages/markitdown[all] \
    /app/packages/markitdown-sample-plugin

# opencv-python (pulled in by the OCR extras) links libGL.so.1 at import time,
# which this image does not ship. The headless wheel is the same module without
# that link; both write the same cv2/ directory, so it has to be swapped in after.
RUN if pip show opencv-python >/dev/null 2>&1; then \
    pip uninstall -y opencv-python && \
    pip --no-cache-dir install --force-reinstall opencv-python-headless; \
    fi

# Default USERID and GROUPID
ARG USERID=nobody
ARG GROUPID=nogroup

USER $USERID:$GROUPID

ENTRYPOINT [ "markitdown" ]

FROM python:3.12-slim

# System libs for Pillow image decoding.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo libopenjp2-7 zlib1g \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The base image has no `git`, so hatch-vcs can't derive the version from
# history — hand it in via the VERSION build arg (the release tag, sans "v").
# Defaults to 0.0.0 for local `docker compose` builds.
ARG VERSION=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${VERSION}

COPY . /app

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && \
    pip install ".[cli,phash]"

# Run-to-completion acquisition job (not a long-lived server). The compose
# `gallery` profile overrides this command with concrete source/limit flags.
ENTRYPOINT ["argus-quarry"]
CMD ["--help"]

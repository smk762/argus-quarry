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

# Extras to install. "server" is in the default set so the published image can
# run the read-only provenance API behind argus-studio's /gallery page.
ARG EXTRAS=cli,phash,server

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && \
    pip install ".[${EXTRAS}]"

# Read-only provenance API (DESIGN.md section 9).
EXPOSE 8102

# Serve by default; keeping ENTRYPOINT means the run-to-completion acquisition
# subcommands still work by overriding the command (e.g. the compose `gallery`
# profile passes concrete source/limit flags).
ENTRYPOINT ["argus-quarry"]
CMD ["serve", "--port", "8102", "--cors"]

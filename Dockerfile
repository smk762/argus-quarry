FROM python:3.12-slim

# System libs for Pillow image decoding.
# eMudhra/emSign (discuss.python.org/t/109576): distrust emSign ECC Root CA - G3
# and the Comodo AAA cross-sign root. pip < 26.2.2 also trusts its own bundle.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libjpeg62-turbo libopenjp2-7 zlib1g \
    && sed -i \
        -e 's|^mozilla/emSign_ECC_Root_CA_-_G3|!mozilla/emSign_ECC_Root_CA_-_G3|' \
        -e 's|^mozilla/Comodo_AAA|!mozilla/Comodo_AAA|' \
        /etc/ca-certificates.conf \
    && update-ca-certificates --fresh >/dev/null \
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
ARG EXTRAS=cli,phash,server,archives

# Hash-pinned wheel so the base image's pip cannot swap the upgrade in transit.
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install \
        "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70" \
    && python -m pip install "certifi>=2026.10.19" ".[${EXTRAS}]"

# Run as a non-root user (issue #8). As root the server writes root-owned
# -wal/-shm sidecars into a host bind mount (issue #9), which then breaks a
# later host-side `fetch` run as a normal user, and an internet-facing service
# has no business defaulting to uid 0. `chown` only the WORKDIR so the default
# ./quarry pool can be created there; a bind-mounted $QUARRY_HOME must itself be
# readable by uid 10001 to serve, and writable by it for the acquisition
# subcommands (see the README).
RUN useradd --uid 10001 --user-group --create-home --shell /usr/sbin/nologin quarry \
    && chown quarry:quarry /app
USER quarry

# Read-only provenance API (DESIGN.md section 9).
EXPOSE 8102

# Serve by default; keeping ENTRYPOINT means the run-to-completion acquisition
# subcommands still work by overriding the command (e.g. the compose `gallery`
# profile passes concrete source/limit flags).
ENTRYPOINT ["argus-quarry"]
CMD ["serve", "--port", "8102", "--cors"]

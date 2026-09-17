# syntax=docker/dockerfile:1

# A Kalam RUNNER: orion-server with the Kalam package and the cartridge component inside it. One
# container plays rated matches for a TinyBrains deployment from any machine, given the address of
# that deployment's Soma and a runner key -- docker-compose.yml at the root is the whole of how.
#
# It is a SERVICE IMAGE, not a carrier of files for somebody else's Orion. Everything a runner needs
# is in it -- the server binary, docker/runner.toml.tmpl, the package and the engine -- and
# docker/entrypoint.sh loads the package into the node at boot. What differs between machines
# arrives by environment: the gate's address, the key, the trust key.
#
# THIS IS WHERE THE ENGINE DIGEST COMES FROM. Orion identifies a plugin by sha256 over the component
# bytes, and three things must agree on it or the ladder silently plays nothing: the plugin Orion
# loads, `[vars] engine_digest` on the runner, and `games.active_engine_digest`, which Soma's
# bootstrap declares from the same ants release. The claim filters on it, so a mismatch is not an
# error anywhere -- the runner simply claims nothing, for ever, and the queue grows.
#
# Which is why the component is TAKEN FROM THE CARTRIDGE'S OWN RELEASE rather than copied into this
# repository and committed. There used to be two copies of one component and nothing errored when
# they drifted apart. There is now one copy, ants' GitHub release, and `ANTS_RELEASE` names it:
#
#   ANTS_RELEASE unset or empty           the latest release, whenever this image builds (the default)
#   ANTS_RELEASE=engine-df312c0458d9      that release, which is what a deployment under a live season wants
#   --build-context ants=../ants/dist     an ants checkout's own build, for an engine not released yet
#
# SIGNATURES ARE NOT IN HERE. The engine's signature belongs to whoever holds the deployment's trust
# key; a runner mounts that deployment's signatures directory at /sig.
#
# EVERY STAGE THAT BUILDS RUNS ON THE BUILD PLATFORM. The cartridge, the generated declarations and
# the orion-server download are the same bytes for every target, so the amd64 and arm64 images carry
# identical packages and one signature verifies on both. Only the runtime stage is per platform.

ARG ANTS_RELEASE=
ARG ORION_VERSION=1.8.1
ARG PYTHON_VERSION=3.12
ARG CURL_VERSION=8.22.0
ARG DEBIAN_VERSION=bookworm-slim

# ---- the cartridge -------------------------------------------------------------
#
# The release unpacked and checked: the viewer inside it must have been transpiled from the component
# beside it, or the archive is not one build. `ants` is the tree alone, laid out as ants' dist/, so
# `--build-context ants=<dist>` replaces this stage with a local build and nothing below can tell.
FROM --platform=$BUILDPLATFORM curlimages/curl:${CURL_VERSION} AS ants-release
USER root
ARG ANTS_RELEASE
# The releases feed changes exactly when a release is published or edited, so ADDing it keys this
# stage's cache: the archive is fetched again after a new release and not otherwise. The archive
# itself is fetched by curl, not ADD or busybox wget: GitHub's download host resolves to four
# addresses, and on a network where one of them is unreachable ADD times out and wget retries that
# same address, while curl moves on to the next. GitHub spells the latest release's URL differently
# from a tag's (`latest/download/<file>` against `download/<tag>/<file>`), which is what the two
# substitutions choose between.
ADD https://github.com/Tiny-Brains/ants/releases.atom /tmp/ants-releases.atom
RUN set -eu; \
    url="https://github.com/Tiny-Brains/ants/releases/${ANTS_RELEASE:+download/}${ANTS_RELEASE:-latest/download}/ants-artifacts.tar.gz"; \
    curl -fsSL --connect-timeout 20 --retry 5 --retry-all-errors -o /tmp/ants-artifacts.tar.gz "$url"; \
    mkdir /artifacts; \
    tar -xzf /tmp/ants-artifacts.tar.gz -C /artifacts; \
    engine="sha256:$(sha256sum /artifacts/tb-ants.wasm | cut -d' ' -f1)"; \
    grep -q "\"engine_digest\": \"${engine}\"" /artifacts/viz/engine.json \
      || { echo "ants ${ANTS_RELEASE:-latest}: viz/ was not transpiled from the component beside it" >&2; exit 1; }; \
    echo "ants ${ANTS_RELEASE:-latest}: engine ${engine}"

FROM scratch AS ants
COPY --from=ants-release /artifacts/ /

# ---- the channel and its workflow --------------------------------------------
#
# `scripts/gen-kalam.py` writes both. The wave's statements live readable in the generator and are
# inlined as single-line JSON strings, because SQL written that way by hand is unreviewable --
# `scripts/check-sql.sh` checks the SHIPPED copy against a real Postgres for exactly that reason.
FROM --platform=$BUILDPLATFORM python:${PYTHON_VERSION}-alpine AS declarations
WORKDIR /src
COPY scripts/gen-kalam.py ./scripts/
RUN mkdir -p channels workflows && python3 scripts/gen-kalam.py

# ---- orion-server, for the target platform -----------------------------------
#
# The upstream release, verified against its published checksum. Fetched on the build platform -- it
# is a download, not a build -- for the architecture the image is for.
FROM --platform=$BUILDPLATFORM debian:${DEBIAN_VERSION} AS orion
ARG ORION_VERSION
ARG TARGETARCH
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /tmp/orion
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) triple=x86_64-unknown-linux-gnu ;; \
      arm64) triple=aarch64-unknown-linux-gnu ;; \
      *) echo "orion-server publishes no ${TARGETARCH} linux build" >&2; exit 1 ;; \
    esac; \
    base="https://github.com/GoPlasmatic/Orion/releases/download/v${ORION_VERSION}"; \
    curl -fsSL --retry 5 -o orion.tar.xz "${base}/orion-server-${triple}.tar.xz"; \
    curl -fsSL --retry 5 -o orion.sha256 "${base}/orion-server-${triple}.tar.xz.sha256"; \
    echo "$(cut -d' ' -f1 orion.sha256)  orion.tar.xz" | sha256sum -c -; \
    tar -xJf orion.tar.xz; \
    install -m 0755 "orion-server-${triple}/orion-server" /usr/local/bin/orion-server

# ---- the runner ----------------------------------------------------------------
FROM debian:${DEBIAN_VERSION}
LABEL org.opencontainers.image.title="kalam" \
      org.opencontainers.image.source="https://github.com/Tiny-Brains/kalam" \
      org.opencontainers.image.description="a TinyBrains runner: orion-server with the Kalam package and the Ants engine, playing rated matches through Soma's gate"

# curl for the healthcheck and the self-load; python3 because load-package.sh stages the set with it
# (stdlib only -- the full interpreter, because python3-minimal ships without `json`).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl python3 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin orion \
 # `models` EXISTS IN THE IMAGE so that a named volume mounted there inherits its ownership. Docker
 # copies the image's directory ownership into a fresh volume when the path is present and creates
 # it ROOT-OWNED when it is not -- and the server runs as uid 10001, so an absent directory here is a
 # model cache the runner cannot write. docker-compose.yml mounts exactly this path.
 && install -d -o orion -g orion /var/lib/orion /var/lib/orion/models

COPY --from=orion /usr/local/bin/orion-server /usr/local/bin/orion-server
COPY docker/entrypoint.sh    /usr/local/bin/kalam
COPY docker/runner.toml.tmpl /etc/orion/runner.toml.tmpl

COPY connectors/             /pkg/kalam/connectors/
COPY shared/                 /pkg/kalam/shared/
COPY scripts/load-package.sh scripts/stage-set.py /pkg/kalam/scripts/
COPY --from=declarations /src/channels/  /pkg/kalam/channels/
COPY --from=declarations /src/workflows/ /pkg/kalam/workflows/
# The engine, its two plugin manifests and the cartridge's registration manifest, from the one ants
# release this image was built with.
COPY --from=ants /tb-ants.wasm /plugin.json /cartridge.json /plugin.toml /pkg/kalam/plugins/tb-ants/

USER orion
EXPOSE 8080

# 200 once startup has finished and the state database answers. A package that failed to load does
# not fail this; the entrypoint's self-load stops the node instead.
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=5 \
  CMD curl -fsS http://127.0.0.1:8080/readyz > /dev/null || exit 1

ENTRYPOINT ["/usr/local/bin/kalam"]

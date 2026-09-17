# syntax=docker/dockerfile:1

# The Kalam package as an ARTIFACT IMAGE: the one cron channel that plays match waves, its workflow,
# its connectors, and the cartridge component it executes.
#
# THIS IS WHERE THE ENGINE DIGEST COMES FROM. Orion identifies a plugin by sha256 over the component
# bytes, and three things must agree on it or the ladder silently plays nothing: the plugin Orion
# loads, `[vars] engine_digest` on the replica, and `games.active_engine_digest`, which pair stamps
# on every row it inserts. The claim filters `engine_digest = $1`, so a mismatch is not an error
# anywhere -- the replica simply claims nothing, for ever, and the queue grows.
#
# Which is why the component is TAKEN FROM THE CARTRIDGE'S OWN RELEASE rather than copied into this
# repository and committed. There used to be two copies of one component -- ants' and the one
# `scripts/vendor-engine.sh` left here -- and nothing errored when they drifted apart: the ladder
# played a component ants does not ship, with a viewer built against the other. It happened once,
# from an edit that changed no behaviour at all. There is now one copy, ants' GitHub release, and
# `ANTS_RELEASE` names it:
#
#   ANTS_RELEASE unset or empty           the latest release, whenever this image builds (the default)
#   ANTS_RELEASE=engine-df312c0458d9      that release, which is what a deployment under a live season wants
#   --build-context ants=../ants/dist     an ants checkout's own build, for an engine not released yet
#
# The latest release is fetched again exactly when a new one is published, and a cached layer is
# used otherwise (the stage below says how). GitHub spells the two URLs differently
# (`latest/download/<file>` against `download/<tag>/<file>`), which is what the two substitutions
# below choose between. The replica
# image still needs no toolchain: what arrives here is the artifact ants already built.

ARG ANTS_RELEASE=
ARG PYTHON_VERSION=3.12
ARG CURL_VERSION=8.22.0
ARG BUSYBOX_VERSION=1.37-musl

# ---- the cartridge -------------------------------------------------------------
#
# The release unpacked and checked: the viewer inside it must have been transpiled from the component
# beside it, or the archive is not one build. `ants` is the tree alone, laid out as ants' dist/, so
# `--build-context ants=<dist>` replaces this stage with a local build and nothing below can tell.
FROM curlimages/curl:${CURL_VERSION} AS ants-release
ARG ANTS_RELEASE
USER root
# The releases feed changes exactly when a release is published or edited, so ADDing it keys this
# stage's cache: the archive is fetched again after a new release and not otherwise. The archive
# itself is fetched by curl, not ADD or busybox wget: GitHub's download host resolves to four
# addresses, and on a network where one of them is unreachable ADD times out and wget retries that
# same address, while curl moves on to the next.
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
FROM python:${PYTHON_VERSION}-alpine AS declarations
WORKDIR /src
COPY scripts/gen-kalam.py ./scripts/
RUN mkdir -p channels workflows && python3 scripts/gen-kalam.py

# ---- the carrier -------------------------------------------------------------
FROM busybox:${BUSYBOX_VERSION}
LABEL org.opencontainers.image.title="kalam package" \
      org.opencontainers.image.source="https://github.com/Tiny-Brains/kalam" \
      org.opencontainers.image.description="the wave channel, its workflow and connectors, and the cartridge component it executes"

COPY connectors/             /artifacts/connectors/
COPY shared/                 /artifacts/shared/
COPY scripts/load-package.sh scripts/stage-set.py /artifacts/scripts/
COPY --from=declarations /src/channels/  /artifacts/channels/
COPY --from=declarations /src/workflows/ /artifacts/workflows/

# cartridge.json and reference/ are not Orion's: they are the registration manifest the platform
# reads for presets, limits and budgets, and the observations admission validates an adapter
# against. Both travel with the component rather than being retyped into [vars] or taken from a
# second copy of the cartridge, so the budget a model is admitted under is the one this engine
# was built with.
COPY --from=ants /tb-ants.wasm /plugin.json /cartridge.json /plugin.toml /artifacts/plugins/tb-ants/
COPY --from=ants /reference/observations.json /artifacts/plugins/tb-ants/reference/

# `docker run --rm -v kalam-pkg:/out tinybrains/kalam:dev` populates a volume with the whole package.
CMD ["sh", "-c", "cp -a /artifacts/. /out/"]

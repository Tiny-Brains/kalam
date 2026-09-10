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
# Which is why the component is TAKEN FROM THE CARTRIDGE'S OWN IMAGE rather than copied into this
# repository and committed. There used to be two copies of one component -- ants' and the one
# `scripts/vendor-engine.sh` left here -- and nothing errored when they drifted apart: the ladder
# played a component ants does not ship, with a viewer built against the other. It happened once,
# from an edit that changed no behaviour at all. There is now one copy, and `ANTS_REF` names it:
#
#   ANTS_REF=tinybrains/ants:dev            whatever is built locally
#   ANTS_REF=ghcr.io/tiny-brains/ants:v1    pinned, which is what a deployment should do
#
# The replica image still needs no toolchain: what arrives here is the artifact ants already built.

ARG ANTS_REF=tinybrains/ants:dev
ARG PYTHON_VERSION=3.12
ARG BUSYBOX_VERSION=1.37-musl

FROM ${ANTS_REF} AS ants

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
COPY scripts/load-package.sh /artifacts/scripts/
COPY --from=declarations /src/channels/  /artifacts/channels/
COPY --from=declarations /src/workflows/ /artifacts/workflows/

# cartridge.json is not Orion's: it is the registration manifest the platform reads for presets,
# limits and budgets, and it travels with the component rather than being retyped into [vars].
COPY --from=ants /artifacts/tb-ants.wasm /artifacts/plugin.json /artifacts/cartridge.json /artifacts/plugins/tb-ants/

# `docker run --rm -v kalam-pkg:/out tinybrains/kalam:dev` populates a volume with the whole package.
CMD ["sh", "-c", "cp -a /artifacts/. /out/"]

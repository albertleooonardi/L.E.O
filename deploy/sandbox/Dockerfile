# The image every agent workspace runs in. sandboxd starts one container per workspace from it, as the
# unprivileged `agent` user with all capabilities dropped; /workspace is a per-workspace volume.
# Copy the self-contained official Node/npm runtime instead of installing Debian's `npm`
# metapackage (which pulls hundreds of unrelated build-time packages).
FROM node:24-slim AS node
FROM python:3.12-slim

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/bin/npm /usr/local/bin/npm
COPY --from=node /usr/local/bin/npx /usr/local/bin/npx
COPY --from=node /usr/local/bin/corepack /usr/local/bin/corepack
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get -o Acquire::Retries=10 -o Acquire::http::Timeout=30 update \
 && apt-get -o Acquire::Retries=10 -o Acquire::http::Timeout=30 install -y --no-install-recommends \
      git ca-certificates
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get -o Acquire::Retries=10 -o Acquire::http::Timeout=30 install -y --no-install-recommends \
      ripgrep procps curl
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get -o Acquire::Retries=10 -o Acquire::http::Timeout=30 install -y --no-install-recommends \
      less file unzip

RUN useradd --uid 1000 --create-home --shell /bin/bash agent \
 && mkdir /workspace && chown agent:agent /workspace \
 # pip packages and scripts are writable in this disposable sandbox; npm globals stay in the agent's home.
 && chown -R agent:agent /usr/local/lib/python3.12/site-packages /usr/local/bin \
 && mkdir -p /home/agent/.npm-global \
 && chown -R agent:agent /home/agent/.npm-global \
 && pip install --no-cache-dir pytest

COPY --chown=agent:agent gitconfig /home/agent/.gitconfig

USER agent
ENV PATH=/home/agent/.npm-global/bin:$PATH \
    NPM_CONFIG_PREFIX=/home/agent/.npm-global \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    TERM=dumb
WORKDIR /workspace
CMD ["sleep", "infinity"]

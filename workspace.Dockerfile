# The agent's computer: one container per conversation (see native/src/modelmesh/docker_workspace.py).
# Build from the repository root:  docker build -t modelmesh-workspace -f workspace.Dockerfile .
# Command-line tools only: Python with uv and pytest, Node.js with npm, git, ripgrep, curl and jq.
FROM node:22-bookworm-slim AS node

FROM python:3.13-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep curl jq util-linux ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv pytest
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm config set --global engine-strict true
COPY native/skills /opt/modelmesh/skills
WORKDIR /workspace

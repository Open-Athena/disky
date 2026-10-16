# Node from the node:22-slim stage (the same Debian base), only for wrangler: the Slack digest's plot upload
# (`wrangler pages deploy`, as hf's and cw-s3's images). Debian's apt nodejs is v20, below wrangler's floor.
FROM node:22-slim AS node

FROM python:3.12-slim
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install -g wrangler@4
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/usr/local UV_NO_CACHE=1 UV_FROZEN=1
COPY pyproject.toml README.md uv.lock ./
COPY src ./src
COPY cloud/pyproject.toml ./cloud/
COPY cloud/src ./cloud/src
RUN uv sync --frozen --no-dev --no-editable --package dt-cloud --extra s3
COPY job/r2.json job/r2-scan-runs.json job/r2-scan job/digest.yml ./job/
# The digest's icons dir: the plot is rendered into it and the dir deployed to the icons project's `plot` branch.
COPY job/icons-r2 ./job/icons-r2
# `scan-run supervise` (the shared job wrapper): the scan runs in its own session; a TERM (preemption) reaches its whole
# group, and a run killed before its own exit (TERM, the OOM killer) is recorded failed and alerts once.
ENTRYPOINT ["dt-cloud", "scan-run", "supervise", "--", "python", "job/r2-scan", "run"]

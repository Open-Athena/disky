# The hf scan job's image: the `disk-tree` + `dt-cloud` CLIs, Nextflow (the scan's head, `dt-cloud scan head
# job/profiles/hf.yml`, and each of its step tasks run this image), the one-VM job script (job/hf-run.sh, until the
# DAG's first clean week), and wrangler (only for the Slack digest's plot upload, `wrangler pages deploy`; D1 writes go
# over the HTTP API). No site build. Built from the minimal context `job/build.sh` stages
# (the repo-root layout, trimmed to what's copied here), as deploy/sheet-mirror/'s.
#
# Installed by uv from the workspace lockfile, `--frozen`, into the image's own interpreter
# (`UV_PROJECT_ENVIRONMENT=/usr/local`): the image runs exactly the pins the tests ran. `--package dt-cloud` pulls
# `disk-tree[gcs,s3]` through dt-cloud's dependency (s3fs for `r2://`).
FROM node:22-slim AS node

# Node from the node:22-slim stage (the same Debian base): Debian's apt nodejs is v20, below wrangler's floor (as
# cw-s3's image).
FROM python:3.12-slim
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install -g wrangler@4
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
# Nextflow (specs/pipelines-everywhere.md), as cw-s3's image: a JRE (Nextflow takes 17–26), `ps` (procps: Nextflow's
# task-metrics wrapper refuses to run without it), the launcher pinned by NXF_VER, and its runtime and the plugins
# `scan head` pins fetched at build time, so neither the head nor a task downloads anything.
ENV NXF_VER=26.10.2 NXF_HOME=/opt/nextflow
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends default-jre-headless procps curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://github.com/nextflow-io/nextflow/releases/download/v$NXF_VER/nextflow" -o /usr/local/bin/nextflow \
    && chmod +x /usr/local/bin/nextflow && nextflow -version \
    && nextflow plugin install nf-google@1.29.1,nf-cloudcache@0.6.2
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/usr/local UV_NO_CACHE=1 UV_FROZEN=1
COPY pyproject.toml README.md uv.lock ./
COPY src ./src
COPY cloud/pyproject.toml ./cloud/
COPY cloud/src ./cloud/src
RUN uv sync --no-dev --no-editable --package dt-cloud --extra s3
# The job reads its D1 and its static-names / interval-store generations from the site's config, so the job and the
# site name the same ones (a change to either reaches the job with the next image).
COPY site/wrangler.toml ./site/wrangler.toml
COPY job/hf-run.sh job/hf-scan-runs.json job/digest.yml ./job/
COPY job/nf ./job/nf
COPY job/profiles ./job/profiles
# The digest's icons dir: the plot is rendered into it and the dir deployed to the icons project's `plot` branch.
COPY job/icons-hf ./job/icons-hf
# The one-VM Batch spec runs `dt-cloud scan-run supervise -- bash job/hf-run.sh` (specs/scan-job-wrapper.md, "The
# entrypoint"): a preemption, cancel or OOM kill is recorded as a failed run. The same here, for a bare `docker run`.
# The head's spec and Nextflow's tasks override it (a task's `containerOptions`: `--entrypoint=/usr/bin/env`).
ENTRYPOINT ["dt-cloud", "scan-run", "supervise", "--", "bash", "job/hf-run.sh"]

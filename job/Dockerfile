# The hf scan job's image (job/hf-run.sh): the `disk-tree` + `dt-cloud` CLIs and the job script, nothing else — no
# site build and no wrangler (D1 writes go over the HTTP API). Built from the minimal context `job/build.sh` stages
# (the repo-root layout, trimmed to what's copied here), as deploy/sheet-mirror/'s.
#
# Installed by uv from the workspace lockfile, `--frozen`, into the image's own interpreter
# (`UV_PROJECT_ENVIRONMENT=/usr/local`): the image runs exactly the pins the tests ran. `--package dt-cloud` pulls
# `disk-tree[gcs,s3]` through dt-cloud's dependency (s3fs for `r2://`).
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
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
COPY job/hf-run.sh job/hf-scan-runs.json ./job/
# The Batch spec runs `dt-cloud scan-run supervise -- bash job/hf-run.sh` (specs/scan-job-wrapper.md, "The
# entrypoint"): a preemption, cancel or OOM kill is recorded as a failed run. The same here, for a bare `docker run`.
ENTRYPOINT ["dt-cloud", "scan-run", "supervise", "--", "bash", "job/hf-run.sh"]

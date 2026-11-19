# The `access-jobs` image (specs/access-job-attribution.md phase 2): Marin's Finelog client at the commit
# `pyproject.toml` pins, in its own environment, apart from the scan image and disky's workspace lock. Built by
# `job/finelog/build.sh` (`dt-cloud image build` over `job/finelog/build.json`); the scan's `access-jobs` step submits it.
FROM python:3.13-slim
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/usr/local UV_NO_CACHE=1 UV_FROZEN=1
COPY job/finelog/pyproject.toml job/finelog/uv.lock ./
RUN uv sync --frozen --no-dev
COPY job/finelog/access_jobs.py ./
ARG DISKY_GIT_SHA=""
ARG DISKY_GIT_DIRTY=""
LABEL org.opencontainers.image.revision=$DISKY_GIT_SHA disky.dirty=$DISKY_GIT_DIRTY
ENV DISKY_GIT_SHA=$DISKY_GIT_SHA DISKY_GIT_DIRTY=$DISKY_GIT_DIRTY
ENTRYPOINT ["python", "/app/access_jobs.py"]

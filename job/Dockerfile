FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/usr/local UV_NO_CACHE=1 UV_FROZEN=1
COPY pyproject.toml README.md uv.lock ./
COPY src ./src
COPY cloud/pyproject.toml ./cloud/
COPY cloud/src ./cloud/src
RUN uv sync --frozen --no-dev --no-editable --package dt-cloud --extra s3
COPY job/r2.json job/r2-scan-runs.json job/r2-scan ./job/
ENTRYPOINT ["python", "job/r2-scan", "run"]

FROM node:24-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS python
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN UV_HTTP_TIMEOUT=300 uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
# Each final stage copies backend/ last, so a source change never invalidates the dependency or apt layers.

# sandboxd: the only service with the Docker socket. It runs agent workspaces and does their git clone and push,
# so it needs git; it runs as root because the socket is root-owned.
FROM python AS sandboxd
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get -o Acquire::Retries=10 -o Acquire::http::Timeout=30 update \
 && apt-get -o Acquire::Retries=10 -o Acquire::http::Timeout=30 install -y --no-install-recommends git ca-certificates
COPY backend/ ./backend/
EXPOSE 8100
CMD ["uvicorn", "backend.sandboxd:create_app", "--factory", "--host", "0.0.0.0", "--port", "8100"]

FROM python AS app
COPY backend/ ./backend/
COPY deploy/start.sh ./deploy/start.sh
COPY --from=frontend /build/dist/ ./frontend/dist/
# A Windows checkout can carry CRLF line endings, which bash refuses to run.
RUN sed -i 's/\r$//' deploy/start.sh && chmod +x deploy/start.sh
RUN useradd --uid 10001 --create-home leo && mkdir /app/.leo && chown leo:leo /app/.leo
USER leo
ENV LEO_DATA_DIR=/app/.leo
EXPOSE 8000
CMD ["uvicorn", "backend.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

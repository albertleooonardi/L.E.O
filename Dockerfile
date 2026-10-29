FROM node:24-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY backend/ ./backend/
COPY --from=frontend /build/dist/ ./frontend/dist/
RUN useradd --uid 10001 --create-home leo && mkdir /app/.leo && chown leo:leo /app/.leo
USER leo
ENV PATH="/app/.venv/bin:$PATH" LEO_DATA_DIR=/app/.leo
EXPOSE 8000
CMD ["uvicorn", "backend.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

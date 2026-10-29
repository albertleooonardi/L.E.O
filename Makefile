.PHONY: install dev frontend check test build
install:
	uv sync --frozen
	npm ci --prefix frontend
	npm run build --prefix frontend
dev:
	uv run --frozen python -m backend.dev
frontend:
	npm run dev --prefix frontend
check:
	uv run --frozen python -m pytest -q
	npm run check --prefix frontend
test:
	uv run --frozen python -m pytest -q
build:
	npm run build --prefix frontend

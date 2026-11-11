.PHONY: install start dev frontend check test build
install:
	./setup.sh
start:
	./start.sh
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

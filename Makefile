# cad-viewer — one instance serves all parts to the tablet + N Claude clients.
.PHONY: serve build dev install

CONFIG ?= ./cad-viewer.toml

install:
	pnpm install
	uv sync --project server

build:
	npm run build            # dist/components.js + dist/index.html

# Start the single long-lived server (leave it running, then open Claude).
serve:
	uv run --project server cad-viewer-serve --config $(CONFIG)

# Frontend hot-reload dev (proxies /api + /ws to the running backend).
dev:
	npm run dev

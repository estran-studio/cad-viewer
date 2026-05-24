# ---------- stage 1: build the Svelte custom-element bundle ----------
FROM node:24-alpine AS frontend
WORKDIR /web

COPY package.json package-lock.json ./
RUN npm ci

COPY vite.config.ts svelte.config.js index.html ./
COPY tsconfig.json tsconfig.app.json tsconfig.node.json ./
COPY public ./public
COPY src ./src
RUN npm run build      # → /web/dist/{components.js, index.html, …}


# ---------- stage 2: runtime (Python + uv + build123d) ----------
# linux/amd64 obligatoire : cadquery-ocp n'a pas de wheel linux/arm64.
# Tourne via Rosetta sur Apple Silicon (OrbStack/Docker Desktop natif).
FROM --platform=linux/amd64 python:3.13-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/server/.venv

# OCP / OpenCascade native deps (slim image strips them) + tini for clean signals.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglu1-mesa libglib2.0-0 libxrender1 libxext6 libsm6 libxi6 \
        tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

# 1) install deps from the lock first → caches the slow OCP/build123d layer
COPY server/pyproject.toml server/uv.lock ./server/
RUN cd server && uv sync --frozen --no-install-project

# 2) copy the server source and finish the sync (installs the project itself,
#    exposing the `cad-viewer-serve` console script)
COPY server/cad_viewer_server ./server/cad_viewer_server
RUN cd server && uv sync --frozen

# 3) bring in the pre-built frontend (served as /dist by the FastAPI app)
COPY --from=frontend /web/dist ./dist

EXPOSE 32325
ENTRYPOINT ["/usr/bin/tini", "--"]
# `cad-viewer.toml` is bind-mounted at /app/cad-viewer.toml (see compose).
CMD ["uv", "run", "--project", "/app/server", "cad-viewer-serve", "--config", "/app/cad-viewer.toml"]

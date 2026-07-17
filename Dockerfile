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
# Multi-arch : pas de `--platform`, l'image suit l'arch de l'hôte.
# Historique : jusqu'à cadquery-ocp 7.8.x il n'existait aucun wheel
# linux/aarch64 (confirmé 2026-05-28), d'où un `--platform=linux/amd64` + Rosetta
# sur Apple Silicon. Réglé depuis 7.9.3.0, qui publie manylinux_2_31_aarch64 ;
# build123d >=0.11 le tire via cadquery-ocp-novtk (le serveur rend ses PNG avec
# matplotlib, jamais VTK → la variante novtk suffit).
# Mesuré sur le loft dense `arrow` : 127 s en arm64 natif contre 303 s sous
# Rosetta. Si un wheel manque pour une arch, forcer `--platform=linux/amd64`.
FROM python:3.13-slim AS runtime
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
#    exposing the `cad-viewer-serve` console script). cad_viewer/ is the
#    part-facing helper package (`from cad_viewer import params`).
COPY server/cad_viewer_server ./server/cad_viewer_server
COPY server/cad_viewer ./server/cad_viewer
RUN cd server && uv sync --frozen

# 3) bring in the pre-built frontend (served as /dist by the FastAPI app)
COPY --from=frontend /web/dist ./dist

EXPOSE 32325
ENTRYPOINT ["/usr/bin/tini", "--"]
# `cad-viewer.toml` is bind-mounted at /app/cad-viewer.toml (see compose).
CMD ["uv", "run", "--project", "/app/server", "cad-viewer-serve", "--config", "/app/cad-viewer.toml"]

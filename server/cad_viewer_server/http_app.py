"""One FastAPI app: studio (dist/), /api/*, /ws, and FastMCP at /mcp.

Same origin → zero CORS. Routes are declared before the static mount so
/api, /ws and /mcp always win. The FastMCP Streamable-HTTP sub-app needs its
session manager running for the whole server lifetime — wired into lifespan.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .builder import PRIO_INTERACTIVE
from .mcp_app import build_mcp
from .state import Registry
from .watcher import ensure_watcher, shutdown_watchers

log = logging.getLogger("cad-viewer")

DIST = Path(__file__).resolve().parents[2] / "dist"


def create_app(registry: Registry) -> FastAPI:
    mcp = build_mcp(registry)
    mcp_asgi = mcp.streamable_http_app()  # also creates mcp.session_manager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        registry.bind_loop(asyncio.get_running_loop())
        registry.builder.start()  # single background build worker
        async with mcp.session_manager.run():  # REQUIRED for the transport
            log.info("mcp: streamable-http session manager up")
            yield
        shutdown_watchers(registry)

    app = FastAPI(title="cad-viewer", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.mcp = mcp

    @app.middleware("http")
    async def no_cache_static(request, call_next):
        # Dev tool: never cache static assets. Combined with the per-build
        # `?v=<timestamp>` query on components.js this guarantees the tablet
        # NEVER serves a stale bundle after a rebuild. ~1.2 MB / reload on
        # LAN is negligible. API/WS/MCP set their own cache headers.
        response = await call_next(request)
        path = request.url.path
        if not (
            path.startswith("/api")
            or path.startswith("/ws")
            or path.startswith("/mcp")
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    def _resolve_or_404(part: str | None):
        ps = registry.resolve(part) if part else registry.single()
        return ps

    @app.get("/api/parts")
    async def api_parts() -> JSONResponse:
        return JSONResponse({"parts": registry.summaries()})

    @app.post("/api/active")
    async def api_active(part: str = Form(...)) -> JSONResponse:
        # Selecting a part on the tablet: open it as a tab + bump its recency.
        ok = registry.touch_active(part)
        if not ok:
            return JSONResponse({"error": "unknown part", "part": part}, status_code=404)
        return JSONResponse({"status": "ok", "part": part, "open": True})

    @app.post("/api/close")
    async def api_close(part: str = Form(...)) -> JSONResponse:
        ok = registry.set_open(part, False)
        if not ok:
            return JSONResponse({"error": "unknown part", "part": part}, status_code=404)
        return JSONResponse({"status": "ok", "part": part, "open": False})

    @app.get("/api/status")
    async def api_status(part: str | None = None) -> JSONResponse:
        ps = _resolve_or_404(part)
        if ps is None:
            return JSONResponse({"error": "unknown part", "part": part}, status_code=404)
        return JSONResponse(ps.status())

    def _lazy_build(ps) -> None:
        # Runs in a thread-pool thread. Watch the part's root for future edits,
        # then enqueue at INTERACTIVE priority and wait — so a tablet load jumps
        # ahead of background rebuilds instead of blocking on `build_lock`.
        ensure_watcher(registry, ps)
        registry.builder.ensure_built(ps, PRIO_INTERACTIVE)

    @app.get("/api/model")
    async def api_model(part: str | None = None) -> Response:
        ps = _resolve_or_404(part)
        if ps is None:
            return JSONResponse({"error": "unknown part", "part": part}, status_code=404)
        if not (ps.loaded and ps.model_bytes is not None):
            await run_in_threadpool(_lazy_build, ps)  # prioritized, non-blocking enqueue
        if ps.model_bytes is None:
            return JSONResponse(
                {"error": "build failed", "part": ps.part_id,
                 "build_error": ps.build_error},
                status_code=404,
            )
        media = "model/gltf-binary" if ps.model_format == "glb" else "model/stl"
        return Response(
            content=ps.model_bytes,
            media_type=media,
            headers={
                "X-Model-Version": str(ps.version),
                "X-Model-Format": ps.model_format,
                "X-Part-Id": ps.part_id,
                "Cache-Control": "no-store",
            },
        )

    @app.post("/api/feedback")
    async def api_feedback(
        image: UploadFile,
        part: str = Form(...),
        note: str = Form(""),
        picked_node: str = Form(""),
        kind: str = Form("annotation"),
    ) -> JSONResponse:
        ps = registry.resolve(part)
        if ps is None:
            return JSONResponse({"error": "unknown part", "part": part}, status_code=404)
        png = await image.read()
        fb = ps.add_feedback(
            png=png, note=note, picked_node=picked_node or None, kind=kind
        )
        return JSONResponse(
            {"status": "ok", "id": fb.id, "part": ps.part_id, "bytes": len(png)}
        )

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        q = registry.hub.register(None)

        async def _reader() -> None:
            # client sends {"type":"subscribe","part":"<id>"} on connect/switch
            while True:
                msg = await websocket.receive_json()
                if msg.get("type") == "subscribe":
                    pid = msg.get("part")
                    registry.hub.subscribe(q, pid)
                    ps = registry.resolve(pid)
                    if ps is not None:
                        await websocket.send_json(
                            {"type": "reload", "version": ps.version,
                             "format": ps.model_format, "part": ps.part_id}
                        )

        async def _writer() -> None:
            while True:
                await websocket.send_json(await q.get())

        try:
            await websocket.send_json({"type": "hello"})
            await asyncio.gather(_reader(), _writer())
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            log.debug("ws closed: %s", exc)
        finally:
            registry.hub.unregister(q)

    # FastMCP sub-app exposes Route("/"); mounted at /mcp it answers at the
    # trailing-slash form. The MCP client URL is therefore .../mcp/ (see
    # CLAUDE.md / ~/.claude.json). Declared before the static mount so it wins.
    app.mount("/mcp", mcp_asgi)

    if DIST.is_dir() and (DIST / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(DIST), html=True), name="app")
        log.info("http: serving frontend from %s", DIST)
    else:

        @app.get("/", response_class=HTMLResponse)
        async def _no_build() -> str:
            return (
                "<h1>cad-viewer backend up</h1><p>Frontend not built. "
                f"Run <code>npm run build</code> in <code>{DIST.parent}</code>.</p>"
            )

        log.warning("http: %s missing — placeholder at /", DIST)

    return app

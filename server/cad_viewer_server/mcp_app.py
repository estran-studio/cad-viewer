"""FastMCP over Streamable HTTP — shared by N Claude Code instances.

Every tool is part-scoped. A Claude instance binds to the part it works on
via `select_part` (cached by MCP session id) or by passing `part=` explicitly
(explicit always wins). Heavy tools (build/render) run in a worker thread so
they never freeze the shared event loop, and serialize on the global build
lock (OCP is not thread-safe).

Tools: list_parts, select_part, open_part, list_feedback,
get_annotated_feedback, get_current_render, rebuild_part, get_part_info.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import time
from pathlib import Path

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ImageContent, TextContent

from .loader import part_info_text
from .render import render_iso_png
from .state import PartState, Registry
from .watcher import build_part, open_part

log = logging.getLogger("cad-viewer")

_MAX_EDGE = 1400  # cap token cost (~w*h/750)


def _png_capped(png: bytes) -> bytes:
    try:
        from PIL import Image as PILImage
    except Exception:  # noqa: BLE001
        return png
    try:
        im = PILImage.open(io.BytesIO(png))
        w, h = im.size
        if max(w, h) <= _MAX_EDGE:
            return png
        s = _MAX_EDGE / max(w, h)
        im = im.resize((int(w * s), int(h * s)), PILImage.LANCZOS)
        out = io.BytesIO()
        im.convert("RGB").save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:  # noqa: BLE001
        return png


def _image(png: bytes) -> ImageContent:
    return ImageContent(
        type="image",
        data=base64.b64encode(_png_capped(png)).decode("ascii"),
        mimeType="image/png",
    )


def _text(s: str) -> TextContent:
    return TextContent(type="text", text=s)


def _session_id(ctx: Context | None) -> str | None:
    try:
        return ctx.request_context.request.headers.get("mcp-session-id")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        return None


def build_mcp(registry: Registry) -> FastMCP:
    # streamable_http_path="/" so mounting the sub-app at "/mcp" yields exactly
    # http://host:port/mcp (not /mcp/mcp).
    mcp = FastMCP("cad-viewer", streamable_http_path="/")

    def _parts_hint() -> str:
        ids = [s["part_id"] for s in registry.summaries()]
        return "Pièces disponibles: " + (", ".join(ids) if ids else "(aucune)")

    def _resolve(part: str | None, ctx: Context | None) -> PartState | None:
        # 1. explicit arg always wins (id or .py path)
        if part:
            return registry.resolve(part)
        # 2. session-bound (set by select_part)
        sid = _session_id(ctx)
        if sid and sid in registry._session_part:
            return registry.parts.get(registry._session_part[sid])
        # 3. exactly one registered part
        return registry.single()

    def _need(part: str | None, ctx: Context | None) -> tuple[PartState | None, str | None]:
        ps = _resolve(part, ctx)
        if ps is None:
            return None, (
                "Pièce non résolue. Passe `part=<id ou chemin .py>` ou appelle "
                "d'abord `select_part`. " + _parts_hint()
            )
        return ps, None

    # ---- registry / selection -------------------------------------------
    @mcp.tool()
    def list_parts() -> str:
        """List every registered part (cheap, no build). Call this first."""
        return json.dumps({"parts": registry.summaries()}, indent=2)

    @mcp.tool()
    async def select_part(part: str, ctx: Context) -> str:
        """Bind THIS Claude session to a part (lazily builds it).

        After this, list_feedback / get_annotated_feedback / get_current_render
        / rebuild_part / get_part_info default to this part for this session.
        """
        ps = registry.resolve(part)
        if ps is None:
            return "Inconnu: " + part + ". " + _parts_hint()
        await anyio.to_thread.run_sync(open_part, registry, ps.part_id)
        sid = _session_id(ctx)
        if sid:
            registry._session_part[sid] = ps.part_id
        st = ps.status()
        return json.dumps(
            {
                "selected": ps.part_id,
                "session_bound": bool(sid),
                "build_ok": st["build_ok"],
                "version": st["version"],
                "build_error": st["build_error"],
            },
            indent=2,
            ensure_ascii=False,
        )

    @mcp.tool(name="open_part")
    async def open_part_tool(part: str) -> str:
        """Lazily build a part WITHOUT binding the session to it."""
        ps = registry.resolve(part)
        if ps is None:
            return "Inconnu: " + part + ". " + _parts_hint()
        await anyio.to_thread.run_sync(open_part, registry, ps.part_id)
        return json.dumps(ps.status(), indent=2, ensure_ascii=False)

    # ---- feedback --------------------------------------------------------
    @mcp.tool()
    def list_feedback(part: str | None = None, ctx: Context = None) -> str:  # type: ignore[assignment]
        """Queued tablet feedback for your part (cheap, no images)."""
        ps, err = _need(part, ctx)
        if err:
            return err
        items = ps.list_feedback()
        pending = [i for i in items if not i["consumed"]]
        return json.dumps(
            {
                "part_id": ps.part_id,
                "pending": len(pending),
                "total": len(items),
                "items": items,
            },
            indent=2,
            ensure_ascii=False,
        )

    @mcp.tool()
    def get_annotated_feedback(
        consume: bool = True, part: str | None = None, ctx: Context = None  # type: ignore[assignment]
    ) -> list:
        """Oldest unconsumed annotated PNG for your part + its note.

        Image = the user's drawing over the frozen 3D view (or a pasted
        reference). `picked_node` is the GLB node they tapped. Empty = not
        an error.
        """
        ps, err = _need(part, ctx)
        if err:
            return [_text(err)]
        fb = ps.take_oldest_unconsumed(consume=consume)
        if fb is None:
            return [_text(f"Aucun feedback annoté en attente pour {ps.part_id}.")]
        meta = {
            "part_id": ps.part_id,
            "id": fb.id,
            "kind": fb.kind,
            "note": fb.note,
            "picked_node": fb.picked_node,
            "picked_nodes": fb.picked_nodes,
            "model_version": fb.model_version,
            "age_s": round(time.time() - fb.created_at, 1),
            "remaining_unconsumed": sum(
                1 for x in ps.list_feedback() if not x["consumed"]
            ),
        }
        return [
            _text(
                "Annotation utilisateur sur la pièce. Analyse les zones "
                "dessinées/encerclées et la note, puis corrige le script "
                "build123d.\n" + json.dumps(meta, indent=2, ensure_ascii=False)
            ),
            _image(fb.png),
        ]

    # ---- reference library ----------------------------------------------
    @mcp.tool()
    def get_references(
        limit: int = 6, part: str | None = None, ctx: Context = None  # type: ignore[assignment]
    ) -> list:
        """Kept reference images for your part (boat photos, drawings, …).

        These persist across sessions — use them to compare your model to the
        target shape WITHOUT the user re-pasting images. Returns the `limit`
        most recent, newest first, each with its label/note.
        """
        ps, err = _need(part, ctx)
        if err:
            return [_text(err)]
        items = list(reversed(registry.references.list(ps.part_id)))
        if not items:
            return [_text(f"Aucune image de référence gardée pour {ps.part_id}.")]
        out: list = [_text(
            f"{len(items)} référence(s) pour {ps.part_id} "
            f"(affichées: {min(limit, len(items))}, plus récentes d'abord)."
        )]
        for it in items[: max(1, limit)]:
            png = registry.references.get_png(ps.part_id, it["id"])
            if png is None:
                continue
            meta = {k: it[k] for k in ("id", "label", "note") if it.get(k)}
            out.append(_text(json.dumps(meta, ensure_ascii=False)))
            out.append(_image(png))
        return out

    @mcp.tool()
    def add_reference_note(
        ref_id: int, note: str, part: str | None = None, ctx: Context = None  # type: ignore[assignment]
    ) -> str:
        """Attach/replace a note on one kept reference image."""
        ps, err = _need(part, ctx)
        if err:
            return err
        ok = registry.references.set_note(ps.part_id, ref_id, note)
        return f"{'OK' if ok else 'introuvable'}: réf #{ref_id} sur {ps.part_id}"

    @mcp.tool()
    async def compare_to_ref(
        ref_id: int | None = None, part: str | None = None, ctx: Context = None  # type: ignore[assignment]
    ) -> list:
        """Current headless iso render + reference image(s) side by side.

        Replaces the manual "render the model, open a ref, eyeball them"
        loop. With `ref_id` compares against that one; otherwise against all
        kept references (newest first).
        """
        ps, err = _need(part, ctx)
        if err:
            return [_text(err)]

        def _render() -> bytes | None:
            open_part(registry, ps.part_id)
            if not ps.build_ok or ps.last_obj is None:
                return None
            with registry.build_lock:
                return render_iso_png(ps.last_obj)

        render = await anyio.to_thread.run_sync(_render)
        out: list = []
        if render is None:
            out.append(_text(f"Render indisponible (build échoué?):\n{ps.build_error}"))
        else:
            out.append(_text("Rendu iso ACTUEL du modèle :"))
            out.append(_image(render))
        items = registry.references.list(ps.part_id)
        if ref_id is not None:
            items = [i for i in items if i["id"] == ref_id]
        else:
            items = list(reversed(items))
        if not items:
            out.append(_text("Aucune référence à comparer."))
        for it in items:
            png = registry.references.get_png(ps.part_id, it["id"])
            if png is None:
                continue
            lbl = it.get("label") or f"réf #{it['id']}"
            note = f" — {it['note']}" if it.get("note") else ""
            out.append(_text(f"RÉFÉRENCE « {lbl} »{note} :"))
            out.append(_image(png))
        return out

    # ---- build / render --------------------------------------------------
    @mcp.tool()
    async def get_current_render(
        part: str | None = None, ctx: Context = None  # type: ignore[assignment]
    ) -> list:
        """Headless iso PNG of your part as it is now (no annotation)."""
        ps, err = _need(part, ctx)
        if err:
            return [_text(err)]

        def _impl() -> list:
            open_part(registry, ps.part_id)
            if not ps.build_ok or ps.last_obj is None:
                return [_text(f"Build échoué:\n\n{ps.build_error}")]
            with registry.build_lock:  # render tessellates → serialize w/ builds
                try:
                    png = render_iso_png(ps.last_obj)
                except Exception as exc:  # noqa: BLE001
                    return [_text(f"Rendu échoué: {type(exc).__name__}: {exc}")]
            txt = json.dumps(ps.status(), indent=2, ensure_ascii=False)
            return [_text(txt), _image(png)]

        return await anyio.to_thread.run_sync(_impl)

    @mcp.tool()
    async def rebuild_part(
        part: str | None = None, ctx: Context = None  # type: ignore[assignment]
    ) -> str:
        """Re-run your part now. Returns status or the Python traceback."""
        ps, err = _need(part, ctx)
        if err:
            return err

        def _impl() -> str:
            ok, msg = build_part(registry, ps)
            return ("OK — " if ok else "ÉCHEC —\n") + msg

        return await anyio.to_thread.run_sync(_impl)

    @mcp.tool()
    def get_part_info(part: str | None = None, ctx: Context = None) -> str:  # type: ignore[assignment]
        """Bounding box / volume / node labels of your part."""
        ps, err = _need(part, ctx)
        if err:
            return err
        st = ps.status()
        return json.dumps(
            {
                "part_id": st["part_id"],
                "part_path": st["part_path"],
                "loaded": st["loaded"],
                "version": st["version"],
                "format": st["format"],
                "build_ok": st["build_ok"],
                "build_error": st["build_error"],
                "bbox_mm": st["bbox"],
                "volume_mm3": st["volume"],
                "node_names": st["node_names"],
            },
            indent=2,
            ensure_ascii=False,
        )

    return mcp

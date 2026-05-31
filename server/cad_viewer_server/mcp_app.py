"""FastMCP over Streamable HTTP — shared by N Claude Code instances.

Every tool is part-scoped. A Claude instance binds to the part it works on
via `select_part` (cached by MCP session id) or by passing `part=` explicitly
(explicit always wins). Heavy tools (build/render) run in a worker thread so
they never freeze the shared event loop, and serialize on the global build
lock (OCP is not thread-safe).

Tools: list_parts, select_part, open_part, list_feedback,
get_annotated_feedback, get_current_render, rebuild_part, get_part_info,
get_references, push_reference, compare_to_ref, add_reference_note,
check_part, list_nodes, measure_distance, cross_section,
get_part_params, set_part_params, apply_param_preset.
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

from .loader import load_part, part_info_text
from .render import render_iso_png, render_views_png
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
            "annotated_reference": (
                {"id": fb.ref_id, "label": fb.ref_label} if fb.ref_id is not None else None
            ),
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
    def push_reference(
        path: str | None = None, image_base64: str | None = None,
        label: str = "", note: str = "", focus: bool = False,
        part: str | None = None, ctx: Context = None,  # type: ignore[assignment]
    ) -> str:
        """Push an image into a part's reference library so the user can OPEN
        and ANNOTATE it on the tablet (e.g. a blueprint you just rendered).

        Provide EITHER `path` (a file on disk — works for paths under the mounted
        project tree, identical host/container path) OR `image_base64` (raw PNG
        bytes, base64; use for files outside mounts like /tmp). `label` names it
        (defaults to the filename). `focus=True` auto-opens it on the tablet;
        else the user gets a toast + a badge on the 🖼 panel. The user's
        annotation comes back via get_annotated_feedback with this ref's label.
        """
        ps, err = _need(part, ctx)
        if err:
            return err
        try:
            if path:
                rec = registry.references.add_from_path(ps.part_id, path, label=label, note=note)
            elif image_base64:
                raw = base64.b64decode(image_base64)
                rec = registry.references.add(ps.part_id, raw, label=label or "image", note=note)
            else:
                return "Fournis `path` ou `image_base64`."
        except FileNotFoundError:
            return f"Fichier introuvable: {path}"
        except Exception as exc:  # noqa: BLE001
            return f"Échec push: {type(exc).__name__}: {exc}"
        registry.hub.publish(ps.part_id, {
            "type": "refs", "part": ps.part_id, "id": rec["id"],
            "label": rec["label"], "focus": bool(focus),
        })
        return json.dumps(
            {"status": "ok", "part": ps.part_id, "id": rec["id"],
             "label": rec["label"], "focus": bool(focus)},
            ensure_ascii=False,
        )

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
            # pool builds don't keep the Shape (not picklable) → build in-process
            with registry.build_lock:
                try:
                    res = load_part(
                        Path(ps.part_path),
                        Path(ps.project_root) if ps.project_root else None,
                        overrides=ps.param_values,
                    )
                    return render_iso_png(res.obj)
                except Exception:  # noqa: BLE001
                    return None

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
        multiview: bool = True, part: str | None = None, ctx: Context = None  # type: ignore[assignment]
    ) -> list:
        """Headless PNG of your part as it is now (no annotation).

        multiview=True (default): one image with iso/front/side/top angles +
        XYZ axes and per-axis size labels (mm) — best to grasp shape AND scale.
        multiview=False: a single clean iso view.
        """
        ps, err = _need(part, ctx)
        if err:
            return [_text(err)]

        def _impl() -> list:
            # pool builds don't keep the Shape → build in-process for the render
            with registry.build_lock:
                try:
                    res = load_part(
                        Path(ps.part_path),
                        Path(ps.project_root) if ps.project_root else None,
                        overrides=ps.param_values,
                    )
                except Exception as exc:  # noqa: BLE001
                    return [_text(f"Build échoué:\n\n{exc}")]
                try:
                    png = (render_views_png(res.obj) if multiview
                           else render_iso_png(res.obj))
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

    # ---- geometry self-check --------------------------------------------
    @mcp.tool()
    async def check_part(
        min_wall_mm: float = 1.2, part: str | None = None, ctx: Context = None  # type: ignore[assignment]
    ) -> str:
        """Validate geometry: bbox, volume, manifold/valid, solid count, and a
        thin-feature heuristic — so the agent confirms a design without guessing.

        `min_wall_mm` flags any solid whose smallest bbox dimension is below it
        (likely too thin to print on FDM). Returns JSON.
        """
        ps, err = _need(part, ctx)
        if err:
            return err

        def _impl() -> str:
            with registry.build_lock:
                try:
                    res = load_part(
                        Path(ps.part_path),
                        Path(ps.project_root) if ps.project_root else None,
                        overrides=ps.param_values,
                    )
                except Exception as exc:  # noqa: BLE001
                    return json.dumps({"ok": False, "build_error": str(exc)})
                obj = res.obj
                report: dict = {"ok": True, "part_id": ps.part_id}
                try:
                    v = obj.is_valid
                    report["is_valid"] = bool(v() if callable(v) else v)
                except Exception:  # noqa: BLE001
                    report["is_valid"] = None
                try:
                    report["is_manifold"] = bool(obj.is_manifold)
                except Exception:  # noqa: BLE001
                    report["is_manifold"] = None
                try:
                    solids = obj.solids()
                except Exception:  # noqa: BLE001
                    solids = []
                report["solid_count"] = len(solids)
                if len(solids) > 1:
                    report["note_solids"] = (
                        f"{len(solids)} solides séparés — assemblage non fusionné "
                        "(normal pour un Compound multi-pièces ; sinon vérifie tes booléens)."
                    )
                report["bbox_mm"] = res.bbox
                report["volume_mm3"] = res.volume
                thin = []
                for i, s in enumerate(solids):
                    try:
                        bb = s.bounding_box()
                        dmin = min(bb.size.X, bb.size.Y, bb.size.Z)
                        if dmin < min_wall_mm:
                            thin.append({"solid": i, "min_dim_mm": round(dmin, 3)})
                    except Exception:  # noqa: BLE001
                        pass
                report["thin_features"] = thin
                report["thin_warning"] = (
                    f"{len(thin)} solide(s) sous {min_wall_mm}mm" if thin else None
                )
                report["watertight"] = report.get("is_valid") and report.get("is_manifold")
                return json.dumps(report, indent=2, ensure_ascii=False)

        return await anyio.to_thread.run_sync(_impl)

    # ---- assembly tree ---------------------------------------------------
    @mcp.tool()
    async def list_nodes(part: str | None = None, ctx: Context = None) -> str:  # type: ignore[assignment]
        """Tree of named nodes (label, bbox mm, volume) of your part's assembly.

        Lets the agent target a node precisely ('the keel', 'port_duct') instead
        of guessing — the same labels the tablet annotation reports as
        picked_node. Returns JSON.
        """
        ps, err = _need(part, ctx)
        if err:
            return err

        def _node(n, depth: int) -> dict:
            d: dict = {"label": getattr(n, "label", "") or "(sans label)"}
            try:
                bb = n.bounding_box()
                d["bbox_mm"] = [round(bb.size.X, 1), round(bb.size.Y, 1), round(bb.size.Z, 1)]
                d["center_mm"] = [round(bb.center().X, 1), round(bb.center().Y, 1), round(bb.center().Z, 1)]
            except Exception:  # noqa: BLE001
                pass
            try:
                if hasattr(n, "volume"):
                    d["volume_mm3"] = round(float(n.volume), 1)
            except Exception:  # noqa: BLE001
                pass
            kids = getattr(n, "children", None)
            if kids and depth < 6:
                d["children"] = [_node(c, depth + 1) for c in kids]
            return d

        def _impl() -> str:
            with registry.build_lock:
                try:
                    res = load_part(
                        Path(ps.part_path),
                        Path(ps.project_root) if ps.project_root else None,
                        overrides=ps.param_values,
                    )
                except Exception as exc:  # noqa: BLE001
                    return json.dumps({"ok": False, "build_error": str(exc)})
                return json.dumps(
                    {"part_id": ps.part_id, "tree": _node(res.obj, 0)},
                    indent=2, ensure_ascii=False,
                )

        return await anyio.to_thread.run_sync(_impl)

    # ---- measure + cross-section ----------------------------------------
    def _find_node(root, label: str):
        match = None
        def walk(n):
            nonlocal match
            if match is not None:
                return
            if (getattr(n, "label", "") or "") == label:
                match = n; return
            for c in (getattr(n, "children", None) or []):
                walk(c)
        walk(root)
        return match

    @mcp.tool()
    async def measure_distance(
        node_a: str, node_b: str, part: str | None = None, ctx: Context = None  # type: ignore[assignment]
    ) -> str:
        """Distance (mm) between the centres of two named nodes + per-axis
        deltas. Node labels come from list_nodes / picked_node."""
        ps, err = _need(part, ctx)
        if err:
            return err

        def _impl() -> str:
            with registry.build_lock:
                try:
                    res = load_part(
                        Path(ps.part_path),
                        Path(ps.project_root) if ps.project_root else None,
                        overrides=ps.param_values,
                    )
                except Exception as exc:  # noqa: BLE001
                    return json.dumps({"ok": False, "build_error": str(exc)})
                na, nb = _find_node(res.obj, node_a), _find_node(res.obj, node_b)
                missing = [n for n, x in ((node_a, na), (node_b, nb)) if x is None]
                if missing:
                    return json.dumps({"ok": False, "error": f"node(s) introuvable(s): {missing}"})
                ca, cb = na.bounding_box().center(), nb.bounding_box().center()
                dx, dy, dz = cb.X - ca.X, cb.Y - ca.Y, cb.Z - ca.Z
                dist = (dx * dx + dy * dy + dz * dz) ** 0.5
                return json.dumps({
                    "ok": True, "node_a": node_a, "node_b": node_b,
                    "distance_mm": round(dist, 3),
                    "delta_mm": {"x": round(dx, 3), "y": round(dy, 3), "z": round(dz, 3)},
                }, indent=2, ensure_ascii=False)

        return await anyio.to_thread.run_sync(_impl)

    @mcp.tool()
    async def cross_section(
        axis: str = "x", offset_mm: float | None = None,
        part: str | None = None, ctx: Context = None  # type: ignore[assignment]
    ) -> list:
        """Cut the model with a plane and render the remaining half — reveals
        internal cavities / wall thicknesses the outer iso hides.

        axis: 'x'|'y'|'z' (plane normal). offset_mm: cut position along that
        axis (default = bbox centre). Returns a multi-view PNG of the cut solid.
        """
        ps, err = _need(part, ctx)
        if err:
            return [_text(err)]
        ax = axis.lower().strip()
        if ax not in ("x", "y", "z"):
            return [_text("axis doit être 'x', 'y' ou 'z'.")]

        def _impl() -> list:
            from build123d import Box, Pos
            with registry.build_lock:
                try:
                    res = load_part(
                        Path(ps.part_path),
                        Path(ps.project_root) if ps.project_root else None,
                        overrides=ps.param_values,
                    )
                except Exception as exc:  # noqa: BLE001
                    return [_text(f"Build échoué:\n\n{exc}")]
                obj = res.obj
                bb = obj.bounding_box()
                ctr = bb.center()
                big = max(bb.size.X, bb.size.Y, bb.size.Z) * 4 + 100
                off = offset_mm if offset_mm is not None else {
                    "x": ctr.X, "y": ctr.Y, "z": ctr.Z}[ax]
                # half-space box positioned to keep the "low" side of the cut
                half = Box(big, big, big)
                shift = {"x": (off - big / 2, ctr.Y, ctr.Z),
                         "y": (ctr.X, off - big / 2, ctr.Z),
                         "z": (ctr.X, ctr.Y, off - big / 2)}[ax]
                try:
                    cut = obj & (Pos(*shift) * half)
                    if cut is None or getattr(cut, "volume", 0) == 0:
                        return [_text("Coupe vide à cet offset — ajuste offset_mm.")]
                    png = render_views_png(cut, views=("iso", "front", "side", "top"))
                except Exception as exc:  # noqa: BLE001
                    return [_text(f"Coupe échouée: {type(exc).__name__}: {exc}")]
            return [_text(f"Coupe {ax}={off:.1f}mm (moitié basse conservée) :"), _image(png)]

        return await anyio.to_thread.run_sync(_impl)

    # ---- parameters ------------------------------------------------------
    @mcp.tool()
    def get_part_params(part: str | None = None, ctx: Context = None) -> str:  # type: ignore[assignment]
        """Parameters this part exposes (cad_viewer.params) + current values."""
        ps, err = _need(part, ctx)
        if err:
            return err
        st = ps.status()
        return json.dumps(
            {"part_id": ps.part_id, "schema": st["param_schema"],
             "values": st["param_values"],
             "presets": registry.params.load_presets(ps.part_id)},
            indent=2, ensure_ascii=False,
        )

    @mcp.tool()
    async def apply_param_preset(
        name: str, part: str | None = None, ctx: Context = None  # type: ignore[assignment]
    ) -> str:
        """Apply a saved named parameter preset and rebuild."""
        ps, err = _need(part, ctx)
        if err:
            return err
        presets = registry.params.load_presets(ps.part_id)
        if name not in presets:
            return f"Preset inconnu: {name}. Dispo: {', '.join(presets) or '(aucun)'}"
        with ps.lock:
            ps.param_values = dict(presets[name])
        registry.params.save(ps.part_id, dict(ps.param_values))

        def _impl() -> str:
            ok, msg = build_part(registry, ps)
            return ("OK — " if ok else "ÉCHEC —\n") + msg

        res = await anyio.to_thread.run_sync(_impl)
        return json.dumps({"applied": name, "result": res, "values": ps.param_values},
                          indent=2, ensure_ascii=False)

    @mcp.tool()
    async def set_part_params(
        values: dict, part: str | None = None, ctx: Context = None  # type: ignore[assignment]
    ) -> str:
        """Set parameter overrides and rebuild. `values` = {name: value, …}.

        Only names declared by the part via cad_viewer.params take effect; the
        part rebuilds with them (persisted across restarts).
        """
        ps, err = _need(part, ctx)
        if err:
            return err
        with ps.lock:
            ps.param_values = {**ps.param_values, **(values or {})}
            new_values = dict(ps.param_values)
        registry.params.save(ps.part_id, new_values)

        def _impl() -> str:
            ok, msg = build_part(registry, ps)
            return ("OK — " if ok else "ÉCHEC —\n") + msg

        res = await anyio.to_thread.run_sync(_impl)
        st = ps.status()
        return json.dumps(
            {"result": res, "values": st["param_values"], "schema": st["param_schema"]},
            indent=2, ensure_ascii=False,
        )

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

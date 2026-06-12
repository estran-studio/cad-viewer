"""Disk-persisted technical-sketch library, per part.

The user draws a dimensioned 2D technical sketch on the tablet (lines, circles,
arcs, beziers + dimensions) and Claude reads it as a DIMENSIONAL source of truth
to write/correct the build123d `.py`. This mirrors `references.py` exactly, but
each item is a *structured document* (`<id>.json`) plus a client-rendered raster
preview (`<id>.png`) for thumbnails and MCP vision.

Layout (per part, dir name = sanitized part_id):
    $CAD_VIEWER_DATA/sketches/<safe_part_id>/
        index.json        # [{id, label, note, created_at, n_entities, n_dims}, ...]
        <id>.json         # the sketch document (entities + dimensions + labels)
        <id>.png          # raster preview rendered client-side at save time

The sketch document is "planegcs-ready": point/line/circle/arc primitives map 1:1
onto FreeCAD's solver primitives so a constraint solver can be bolted on later
without reshaping stored data. `bezier`/`ink` are the freeform (no-solver) layer.

`CAD_VIEWER_DATA` defaults to ~/.cad-viewer; in Docker it is a mounted volume so
the library persists across image rebuilds (see docker-compose.yml).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("cad-viewer")

_CAP_DEFAULT = 24  # keep the N most recent sketches per part; drop the oldest


def _data_root() -> Path:
    base = os.environ.get("CAD_VIEWER_DATA")
    root = Path(base) if base else Path.home() / ".cad-viewer"
    return root / "sketches"


def _safe(part_id: str) -> str:
    """part_id has slashes — flatten to one filesystem-safe directory name."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", part_id) or "_"


def _counts(doc: dict) -> tuple[int, int]:
    ents = doc.get("entities") or []
    dims = doc.get("dimensions") or []
    return (len(ents) if isinstance(ents, list) else 0,
            len(dims) if isinstance(dims, list) else 0)


class SketchStore:
    def __init__(self, root: Path | None = None, cap: int = _CAP_DEFAULT) -> None:
        self.root = root or _data_root()
        self.cap = cap
        self._lock = threading.Lock()

    def _dir(self, part_id: str) -> Path:
        return self.root / _safe(part_id)

    def _load_index(self, part_id: str) -> list[dict]:
        p = self._dir(part_id) / "index.json"
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text())
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_index(self, part_id: str, items: list[dict]) -> None:
        d = self._dir(part_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.json").write_text(json.dumps(items, indent=2))

    # ---- public API ------------------------------------------------------
    def add(self, part_id: str, doc: dict, png: bytes | None = None,
            label: str = "", note: str = "") -> dict:
        with self._lock:
            items = self._load_index(part_id)
            next_id = max((i["id"] for i in items), default=0) + 1
            d = self._dir(part_id)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{next_id}.json").write_text(json.dumps(doc, indent=2))
            if png:
                (d / f"{next_id}.png").write_bytes(png)
            n_ent, n_dim = _counts(doc)
            rec = {
                "id": next_id,
                "label": label or "",
                "note": note or "",
                "created_at": time.time(),
                "n_entities": n_ent,
                "n_dims": n_dim,
            }
            items.append(rec)
            while len(items) > self.cap:  # evict oldest
                old = items.pop(0)
                for ext in ("json", "png"):
                    try:
                        (d / f"{old['id']}.{ext}").unlink()
                    except OSError:
                        pass
            self._save_index(part_id, items)
        log.info("[%s] sketch #%d saved (%d ent, %d dim)", part_id, rec["id"], n_ent, n_dim)
        return rec

    def update(self, part_id: str, sketch_id: int, doc: dict,
               png: bytes | None = None) -> dict | None:
        with self._lock:
            items = self._load_index(part_id)
            rec = next((i for i in items if i["id"] == int(sketch_id)), None)
            if rec is None:
                return None
            d = self._dir(part_id)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{int(sketch_id)}.json").write_text(json.dumps(doc, indent=2))
            if png:
                (d / f"{int(sketch_id)}.png").write_bytes(png)
            rec["n_entities"], rec["n_dims"] = _counts(doc)
            rec["updated_at"] = time.time()
            self._save_index(part_id, items)
        return rec

    def list(self, part_id: str) -> list[dict]:
        with self._lock:
            return self._load_index(part_id)

    def get_doc(self, part_id: str, sketch_id: int) -> dict | None:
        p = self._dir(part_id) / f"{int(sketch_id)}.json"
        try:
            return json.loads(p.read_text()) if p.exists() else None
        except (OSError, ValueError):
            return None

    def get_png(self, part_id: str, sketch_id: int) -> bytes | None:
        p = self._dir(part_id) / f"{int(sketch_id)}.png"
        try:
            return p.read_bytes() if p.exists() else None
        except OSError:
            return None

    def meta(self, part_id: str, sketch_id: int) -> dict | None:
        with self._lock:
            items = self._load_index(part_id)
            return next((i for i in items if i["id"] == int(sketch_id)), None)

    def delete(self, part_id: str, sketch_id: int) -> bool:
        with self._lock:
            items = self._load_index(part_id)
            kept = [i for i in items if i["id"] != int(sketch_id)]
            if len(kept) == len(items):
                return False
            self._save_index(part_id, kept)
            for ext in ("json", "png"):
                try:
                    (self._dir(part_id) / f"{int(sketch_id)}.{ext}").unlink()
                except OSError:
                    pass
            return True

    def set_note(self, part_id: str, sketch_id: int, note: str) -> bool:
        with self._lock:
            items = self._load_index(part_id)
            for i in items:
                if i["id"] == int(sketch_id):
                    i["note"] = note or ""
                    self._save_index(part_id, items)
                    return True
            return False

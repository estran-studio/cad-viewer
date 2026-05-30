"""Disk-persisted reference-image library, per part.

The user pastes/keeps reference photos or drawings (boat side views, etc.) and
iterates against them across many sessions. Unlike the ephemeral in-RAM
`PartState.feedback_queue`, references are LONG-LIVED: stored on disk so they
survive server restarts, and exposed to Claude via the `get_references` MCP tool
— so the user no longer has to drag images into the chat by hand every time.

Layout (per part, dir name = sanitized part_id):
    $CAD_VIEWER_DATA/references/<safe_part_id>/
        index.json        # [{id, label, note, created_at, bytes}, ...]
        <id>.png          # the image bytes

`CAD_VIEWER_DATA` defaults to ~/.cad-viewer; in Docker it is set to a mounted
volume so the library persists across image rebuilds (see docker-compose.yml).
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

_CAP_DEFAULT = 12  # keep the N most recent refs per part; drop the oldest


def _data_root() -> Path:
    base = os.environ.get("CAD_VIEWER_DATA")
    root = Path(base) if base else Path.home() / ".cad-viewer"
    return root / "references"


def _safe(part_id: str) -> str:
    """part_id has slashes — flatten to one filesystem-safe directory name."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", part_id) or "_"


class ReferenceStore:
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
    def add(self, part_id: str, png: bytes, label: str = "", note: str = "") -> dict:
        with self._lock:
            items = self._load_index(part_id)
            next_id = max((i["id"] for i in items), default=0) + 1
            d = self._dir(part_id)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{next_id}.png").write_bytes(png)
            rec = {
                "id": next_id,
                "label": label or "",
                "note": note or "",
                "created_at": time.time(),
                "bytes": len(png),
            }
            items.append(rec)
            while len(items) > self.cap:  # evict oldest
                old = items.pop(0)
                try:
                    (d / f"{old['id']}.png").unlink()
                except OSError:
                    pass
            self._save_index(part_id, items)
        log.info("[%s] reference #%d kept (%d bytes)", part_id, rec["id"], len(png))
        return rec

    def list(self, part_id: str) -> list[dict]:
        with self._lock:
            return self._load_index(part_id)

    def get_png(self, part_id: str, ref_id: int) -> bytes | None:
        p = self._dir(part_id) / f"{int(ref_id)}.png"
        try:
            return p.read_bytes() if p.exists() else None
        except OSError:
            return None

    def delete(self, part_id: str, ref_id: int) -> bool:
        with self._lock:
            items = self._load_index(part_id)
            kept = [i for i in items if i["id"] != int(ref_id)]
            if len(kept) == len(items):
                return False
            self._save_index(part_id, kept)
            try:
                (self._dir(part_id) / f"{int(ref_id)}.png").unlink()
            except OSError:
                pass
            return True

    def set_calibration(self, part_id: str, ref_id: int, px_per_mm: float) -> bool:
        """Store a scale (pixels of the natural image per millimetre) on a ref."""
        with self._lock:
            items = self._load_index(part_id)
            for i in items:
                if i["id"] == int(ref_id):
                    i["px_per_mm"] = float(px_per_mm)
                    self._save_index(part_id, items)
                    return True
            return False

    def set_note(self, part_id: str, ref_id: int, note: str) -> bool:
        with self._lock:
            items = self._load_index(part_id)
            for i in items:
                if i["id"] == int(ref_id):
                    i["note"] = note or ""
                    self._save_index(part_id, items)
                    return True
            return False

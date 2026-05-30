"""Persist per-part parameter overrides on disk.

Lives under $CAD_VIEWER_DATA/params/<safe_part_id>.json (same writable volume as
the reference library) — NOT next to the part .py, because the parts tree is
bind-mounted read-only in Docker. Survives restarts; one file per part, human
readable.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path

log = logging.getLogger("cad-viewer")


def _root() -> Path:
    base = os.environ.get("CAD_VIEWER_DATA")
    r = Path(base) if base else Path.home() / ".cad-viewer"
    return r / "params"


def _safe(part_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", part_id) or "_"


class ParamStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or _root()
        self._lock = threading.Lock()

    def load(self, part_id: str) -> dict:
        p = self.root / f"{_safe(part_id)}.json"
        if not p.exists():
            return {}
        try:
            d = json.loads(p.read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, part_id: str, values: dict) -> None:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / f"{_safe(part_id)}.json").write_text(
                json.dumps(values, indent=2)
            )

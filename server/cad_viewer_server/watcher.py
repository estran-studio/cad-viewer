"""Lazy build + one filesystem watcher per project root.

`open_part` builds a part on first use and caches it. A single `RootWatcher`
per distinct project root rebuilds every *loaded* part under it on any .py
change (so editing shared `fleet_cad/params.py` refreshes all open parts, and
editing one part file refreshes that part). Every build holds the global
`build_lock` — OCP/build123d is not thread-safe.
"""

from __future__ import annotations

import logging
import threading
import traceback
from pathlib import Path

from watchfiles import Change, watch

from .loader import PartError, load_part
from .state import PartState, Registry, root_of

log = logging.getLogger("cad-viewer")

_IGNORE_DIRS = {
    ".venv", "venv", "__pycache__", ".git", "node_modules",
    "exports", "export", "dist", ".exchange-cache", ".mypy_cache",
}


def _py_filter(change: Change, path: str) -> bool:
    p = Path(path)
    if any(part in _IGNORE_DIRS for part in p.parts):
        return False
    return p.suffix == ".py"


def build_part(registry: Registry, ps: PartState) -> tuple[bool, str]:
    """(Re)run one part under the global build lock. Updates state + WS."""
    with registry.build_lock:
        ps.building = True
        try:
            res = load_part(
                Path(ps.part_path),
                Path(ps.project_root) if ps.project_root else None,
            )
        except PartError as exc:
            ps.set_build_error(str(exc))
            ps.building = False
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
            ps.set_build_error(msg)
            ps.building = False
            return False, msg
        v = ps.set_model(
            res.model_bytes,
            res.model_format,
            obj=res.obj,
            node_names=res.node_names,
            bbox=res.bbox,
            volume=res.volume,
        )
        ps.building = False
        return True, f"built v{v} ({res.model_format}, {len(res.model_bytes)} bytes)"


def open_part(registry: Registry, ref: str) -> PartState:
    """Resolve + lazily build + ensure a watcher. Returns the PartState.

    Idempotent: an already-loaded part returns immediately (instant switch).
    A build failure is reflected on the PartState (build_error), not raised.
    """
    ps = registry.resolve(ref)
    if ps is None:
        raise KeyError(ref)
    if not (ps.loaded and ps.model_bytes is not None):
        build_part(registry, ps)
    ensure_watcher(registry, ps)
    return ps


def ensure_watcher(registry: Registry, ps: PartState) -> None:
    root = root_of(ps)
    with registry.lock:
        w = registry._watchers.get(root)
        if w is not None and w.is_alive():  # type: ignore[attr-defined]
            return
        w = RootWatcher(registry, root)
        registry._watchers[root] = w
    w.start()


def shutdown_watchers(registry: Registry) -> None:
    with registry.lock:
        watchers = list(registry._watchers.values())
    for w in watchers:
        w.stop()  # type: ignore[attr-defined]


class RootWatcher(threading.Thread):
    """Watches one project root; rebuilds all loaded parts under it on change."""

    def __init__(self, registry: Registry, root: str) -> None:
        super().__init__(name=f"watch:{root}", daemon=True)
        self.registry = registry
        self.root = root
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("watching %s", self.root)
        try:
            for changes in watch(
                self.root,
                watch_filter=_py_filter,
                stop_event=self._stop,
                debounce=400,
                step=120,
            ):
                files = sorted({Path(p).name for _, p in changes})
                targets = self.registry.loaded_parts_under(self.root)
                if not targets:
                    continue
                log.info(
                    "change (%s) → rebuilding %d part(s) under %s",
                    ", ".join(files), len(targets), self.root,
                )
                for ps in targets:
                    ok, msg = build_part(self.registry, ps)
                    log.info("[%s] %s", ps.part_id, msg.splitlines()[0])
        except Exception as exc:  # noqa: BLE001
            log.exception("watcher %s crashed: %s", self.root, exc)

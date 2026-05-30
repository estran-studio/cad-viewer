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
import time
import traceback
from pathlib import Path

from watchfiles import Change, watch

from .builder import PRIO_BACKGROUND, PRIO_EDIT
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
    """(Re)run one part under the global build lock. Updates state + WS.

    The single build worker (builder.BuildQueue) is the normal caller, but MCP
    tools (rebuild_part / get_current_render / select_part→open_part) still call
    this directly on their own thread — `build_lock` keeps all of them serial.
    `mark_build_done` clears `building` and wakes anyone in `ensure_built`.
    """
    with registry.build_lock:
        with ps.lock:
            ps.building = True
            ps.build_started_at = time.time()
        try:
            res = load_part(
                Path(ps.part_path),
                Path(ps.project_root) if ps.project_root else None,
            )
        except PartError as exc:
            ps.set_build_error(str(exc))
            ps.mark_build_done()
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
            ps.set_build_error(msg)
            ps.mark_build_done()
            return False, msg
        v = ps.set_model(
            res.model_bytes,
            res.model_format,
            obj=res.obj,
            node_names=res.node_names,
            bbox=res.bbox,
            volume=res.volume,
        )
        ps.mark_build_done()
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
                targets = self.registry.loaded_parts_under(self.root)
                if not targets:
                    continue

                # Directory-scoped rebuilds. Editing one part's .py (or a
                # non-part helper sitting in the same dir, e.g. arrow_geom.py)
                # rebuilds only the loaded parts in THAT directory — not all 45
                # loaded parts, which was the serial "build forever" storm.
                # A change OUTSIDE every part dir (a shared fleet_cad/* helper)
                # still rebuilds everything, because any part may import it.
                changed: set[Path] = set()
                for _c, p in changes:
                    try:
                        changed.add(Path(p).resolve())
                    except OSError:
                        pass
                changed_dirs = {c.parent for c in changed}
                part_dirs = {Path(ps.part_path).parent for ps in targets}
                shared = any(d not in part_dirs for d in changed_dirs)
                if shared:
                    to_build, reason = targets, "shared helper"
                else:
                    to_build = [
                        ps
                        for ps in targets
                        if Path(ps.part_path).parent in changed_dirs
                    ]
                    reason = "part dir"
                if not to_build:
                    continue

                names = ", ".join(sorted({c.name for c in changed}))
                log.info(
                    "change (%s) → queue %d/%d part(s) under %s [%s]",
                    names, len(to_build), len(targets), self.root, reason,
                )
                # The part whose own file changed builds first (PRIO_EDIT);
                # collateral siblings/shared rebuilds run in the background.
                for ps in to_build:
                    prio = (
                        PRIO_EDIT
                        if Path(ps.part_path) in changed
                        else PRIO_BACKGROUND
                    )
                    self.registry.builder.request(ps, prio)
        except Exception as exc:  # noqa: BLE001
            log.exception("watcher %s crashed: %s", self.root, exc)

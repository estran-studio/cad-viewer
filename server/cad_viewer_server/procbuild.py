"""Out-of-process build pool.

build123d/OCP is not thread-safe, but it IS process-safe — so to actually build
in parallel (different parts / param combos / prewarm across cores) we run each
build in a separate process. A bonus: every build starts in a fresh interpreter,
so the multi-`hull.py` sibling-name collisions can't leak across builds.

Workers use the 'spawn' start method (clean child, no inherited locks/threads
from the uvicorn process). The trade-off is a one-time build123d import per
worker (~seconds, esp. under Rosetta); workers stay warm afterwards. The result
crossing the process boundary is the GLB bytes + geometry stats (all picklable)
— NOT the build123d Shape, which is why pool-built parts have last_obj=None and
get_current_render rebuilds in-process when it needs the object to tessellate.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import threading
from concurrent.futures import Future, ProcessPoolExecutor

log = logging.getLogger("cad-viewer")


def _worker_build(part_path: str, project_root: str | None, overrides: dict | None) -> dict:
    """Runs in a worker process. Returns a picklable payload (no Shape)."""
    from pathlib import Path

    from .loader import PartError, load_part

    try:
        res = load_part(
            Path(part_path),
            Path(project_root) if project_root else None,
            overrides=overrides,
        )
        return {
            "ok": True,
            "data": res.model_bytes,
            "fmt": res.model_format,
            "nodes": res.node_names,
            "bbox": res.bbox,
            "volume": res.volume,
            "schema": res.param_schema,
        }
    except PartError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        import traceback
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"}


def default_workers() -> int:
    env = os.environ.get("CAD_VIEWER_BUILD_WORKERS", "")
    if env.isdigit() and int(env) > 0:
        return int(env)
    return max(1, (os.cpu_count() or 2) - 2)


class BuildPool:
    def __init__(self, workers: int | None = None) -> None:
        self.workers = workers or default_workers()
        self._ex: ProcessPoolExecutor | None = None
        self._lock = threading.Lock()

    def ensure(self) -> ProcessPoolExecutor:
        with self._lock:
            if self._ex is None:
                ctx = multiprocessing.get_context("spawn")
                self._ex = ProcessPoolExecutor(max_workers=self.workers, mp_context=ctx)
                log.info("build pool: %d worker process(es) (spawn)", self.workers)
            return self._ex

    def submit(self, part_path: str, project_root: str | None, overrides: dict | None) -> Future:
        return self.ensure().submit(_worker_build, part_path, project_root, overrides)

    def shutdown(self) -> None:
        with self._lock:
            if self._ex is not None:
                self._ex.shutdown(wait=False, cancel_futures=True)
                self._ex = None

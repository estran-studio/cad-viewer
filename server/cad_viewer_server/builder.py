"""Single background build worker + priority queue.

build123d/OCP is not thread-safe, so every build still runs strictly one at a
time under `registry.build_lock`. The point of this queue is to **decouple
requesting a build from running it**: an HTTP request, the filesystem watcher or
an MCP tool enqueues a build and returns instead of holding the lock for the
whole duration. Three consequences fix the "the app freezes while one part
builds" pain (see memory cad-viewer-workflow-frictions):

  * the part the user is actively loading is built FIRST (PRIO_INTERACTIVE),
    jumping ahead of collateral background rebuilds (PRIO_BACKGROUND);
  * already-built parts are served instantly (the cached path in /api/model
    never touches the queue or the lock);
  * a single worker drains the queue, so builds stay serialized without every
    caller blocking on `build_lock` from its own thread.

Coalescing: a part queued twice collapses to one build (the latest wins); a
re-request at higher priority overtakes a pending lower-priority one.
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state import PartState, Registry

log = logging.getLogger("cad-viewer")

# Lower number = higher priority.
PRIO_INTERACTIVE = 0   # tablet/MCP is actively loading this part — build now
PRIO_EDIT = 5          # the part whose own .py the user just saved
PRIO_BACKGROUND = 10   # collateral rebuilds (shared helper changed, etc.)
PRIO_PREWARM = 20      # speculative builds of other param combos (lowest)


class BuildQueue:
    def __init__(self, registry: "Registry") -> None:
        self.registry = registry
        self._q: "queue.PriorityQueue" = queue.PriorityQueue()
        self._seq = itertools.count()
        self._queued: dict[str, int] = {}  # part_id -> best priority pending
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(
                target=self._run, name="build-worker", daemon=True
            )
            self._worker.start()

    def request(
        self,
        ps: "PartState",
        priority: int = PRIO_BACKGROUND,
        values: dict | None = None,   # None → build the part's CURRENT values
        display: bool = True,         # False → prewarm: build+cache, don't show
    ) -> None:
        """Enqueue a (re)build of `ps`. Returns immediately. Coalesces."""
        self.start()  # defensive: idempotent, so request always works
        vkey = "<current>" if values is None else ps._cache_key(values)
        qkey = (ps.part_id, vkey, display)
        with self._lock:
            prev = self._queued.get(qkey)
            if prev is not None and prev <= priority:
                return  # already pending at the same or higher priority
            self._queued[qkey] = priority if prev is None else min(prev, priority)
            self._q.put((priority, next(self._seq), ps.part_id, values, display))
        if display:
            ps.mark_build_pending()

    def _schedule_prewarm(self, ps: "PartState") -> None:
        """After a visible build, speculatively build the part's OTHER enum
        values into the cache so switching to them later is instant."""
        enum = next((p for p in ps.param_schema if p.get("type") == "enum"), None)
        if not enum:
            return
        current = dict(ps.param_values)
        for opt in enum.get("options", []):
            vals = {**current, enum["name"]: opt}
            if ps.cache_get(vals) is None:
                self.request(ps, PRIO_PREWARM, values=vals, display=False)

    def ensure_built(
        self,
        ps: "PartState",
        priority: int = PRIO_INTERACTIVE,
        timeout: float = 180.0,
    ) -> None:
        """Block until `ps` has been built at least once from now, or timeout.

        MUST be called from a worker/thread-pool thread (it blocks). Returns
        immediately if the part already has bytes to serve.
        """
        if ps.loaded and ps.model_bytes is not None:
            return
        start = ps.build_attempts
        self.request(ps, priority)
        deadline = time.time() + timeout
        with ps.build_cv:
            while ps.build_attempts == start and time.time() < deadline:
                ps.build_cv.wait(timeout=max(0.05, deadline - time.time()))

    def rebuild_and_wait(self, ps: "PartState", timeout: float = 180.0) -> None:
        """Force a fresh build of the CURRENT combo (ignores cache) and wait."""
        start = ps.build_attempts
        ps.cache_clear()
        self.request(ps, PRIO_INTERACTIVE)
        deadline = time.time() + timeout
        with ps.build_cv:
            while ps.build_attempts == start and time.time() < deadline:
                ps.build_cv.wait(timeout=max(0.05, deadline - time.time()))

    def _run(self) -> None:
        # Dispatcher: pop in priority order, submit to the process pool, and
        # throttle to pool size. OCP runs in the workers (parallel); we only
        # apply the picklable result back here (set_model / cache / errors).
        sem = threading.Semaphore(self.registry.build_pool.workers)
        while True:
            _prio, _seq, part_id, values, display = self._q.get()
            ps = self.registry.parts.get(part_id)
            if ps is None:
                continue
            vals = ps.param_values if values is None else values
            vkey = "<current>" if values is None else ps._cache_key(values)
            qkey = (part_id, vkey, display)
            with self._lock:
                if self._queued.get(qkey) is None:
                    continue  # stale duplicate already handled
                self._queued.pop(qkey, None)

            # Cache fast-path — no subprocess needed.
            hit = ps.cache_get(vals)
            if hit is not None:
                if display:
                    self._apply_entry(ps, hit)
                    self._schedule_prewarm(ps)
                continue

            sem.acquire()  # block when pool-size builds are already in flight
            try:
                fut = self.registry.build_pool.submit(ps.part_path, ps.project_root, vals)
            except Exception:  # noqa: BLE001 — broken pool → reset so it recreates
                sem.release()
                log.exception("submit build failed on %s; resetting pool", part_id)
                self.registry.build_pool.shutdown()
                continue
            fut.add_done_callback(
                lambda f, ps=ps, vals=vals, display=display: self._apply(f, ps, vals, display, sem)
            )

    def _apply_entry(self, ps: "PartState", entry: dict) -> None:
        ps.set_model(
            entry["data"], entry["fmt"], obj=entry.get("obj"),
            node_names=entry["nodes"], bbox=entry["bbox"], volume=entry["volume"],
            param_schema=entry["schema"],
        )
        ps.mark_build_done()

    def _apply(self, future, ps: "PartState", vals: dict, display: bool, sem) -> None:
        # Done-callbacks for a ProcessPoolExecutor run serially in its result
        # thread, so set_model/cache mutations here never race each other.
        try:
            payload = future.result()
        except Exception as exc:  # noqa: BLE001 — worker process died, etc.
            payload = {"ok": False, "error": f"build process error: {exc}"}
            self.registry.build_pool.shutdown()  # poisoned pool → recreate next time
        finally:
            sem.release()
        try:
            if payload.get("ok"):
                entry = {
                    "data": payload["data"], "fmt": payload["fmt"], "obj": None,
                    "nodes": payload["nodes"], "bbox": payload["bbox"],
                    "volume": payload["volume"], "schema": payload["schema"],
                }
                ps.cache_put(vals, entry)
                if display:
                    self._apply_entry(ps, entry)
                    self._schedule_prewarm(ps)
            elif display:
                ps.set_build_error(payload.get("error", "build failed"))
                ps.mark_build_done()
        except Exception:  # noqa: BLE001
            log.exception("apply build result failed on %s", ps.part_id)

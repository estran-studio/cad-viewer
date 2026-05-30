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

    def request(self, ps: "PartState", priority: int = PRIO_BACKGROUND) -> None:
        """Enqueue a (re)build of `ps`. Returns immediately. Coalesces."""
        self.start()  # defensive: idempotent, so request always works
        with self._lock:
            prev = self._queued.get(ps.part_id)
            if prev is not None and prev <= priority:
                return  # already pending at the same or higher priority
            self._queued[ps.part_id] = priority if prev is None else min(prev, priority)
            self._q.put((priority, next(self._seq), ps.part_id))
        ps.mark_build_pending()

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

    def _run(self) -> None:
        # Lazy import avoids an import cycle (watcher imports state, state owns us).
        from .watcher import build_part

        while True:
            _prio, _seq, part_id = self._q.get()
            with self._lock:
                if self._queued.get(part_id) is None:
                    continue  # stale duplicate already handled
                self._queued.pop(part_id, None)
            ps = self.registry.parts.get(part_id)
            if ps is None:
                continue
            try:
                build_part(self.registry, ps)
            except Exception:  # noqa: BLE001 — never let the worker die
                log.exception("build worker error on %s", part_id)

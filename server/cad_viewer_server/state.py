"""Process-wide registry of parts.

One long-lived process serves N parts to the tablet AND N Claude clients over
HTTP MCP, all touching the same in-memory `Registry`. Each part owns its build
output + its own feedback queue + its own lock. A single global `build_lock`
serializes every build (OCP/build123d is not thread-safe). The shared `Hub`
fans WS reload events out per-part (a tablet on part A never gets part B).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("cad-viewer")


@dataclass
class Feedback:
    """One piece of feedback drawn (or pasted) by the user on the tablet."""

    id: int
    created_at: float
    png: bytes
    note: str = ""
    picked_node: str | None = None       # first picked node (back-compat)
    picked_nodes: list[str] = field(default_factory=list)  # all circled regions
    model_version: int = 0
    kind: str = "annotation"  # "annotation" | "reference"
    part_id: str = ""
    consumed: bool = False

    def summary(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "age_s": round(time.time() - self.created_at, 1),
            "has_note": bool(self.note),
            "note_preview": (self.note[:80] + "…") if len(self.note) > 80 else self.note,
            "picked_node": self.picked_node,
            "picked_nodes": self.picked_nodes,
            "model_version": self.model_version,
            "kind": self.kind,
            "part_id": self.part_id,
            "consumed": self.consumed,
            "png_bytes": len(self.png),
        }


class Hub:
    """Per-part fan-out of reload events to connected websockets.

    `publish(part_id, msg)` is called from a watcher thread; it hops onto the
    uvicorn loop with `call_soon_threadsafe` and only feeds queues whose
    current subscription matches `part_id`.
    """

    def __init__(self) -> None:
        self._clients: dict[asyncio.Queue, str | None] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def register(self, part_id: str | None = None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._clients[q] = part_id
        return q

    def subscribe(self, q: asyncio.Queue, part_id: str | None) -> None:
        if q in self._clients:
            self._clients[q] = part_id

    def unregister(self, q: asyncio.Queue) -> None:
        self._clients.pop(q, None)

    def publish(self, part_id: str, message: dict) -> None:
        """Thread-safe, best-effort. Never raises into the watcher thread."""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._fan_out, part_id, message)
        except RuntimeError:
            pass  # event loop closed/closing

    def _fan_out(self, part_id: str, message: dict) -> None:
        for q, sub in list(self._clients.items()):
            if sub != part_id:
                continue
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                pass  # slow client; it re-fetches /api/model on reconnect


@dataclass
class PartState:
    """Everything about one build123d part. Mutated under `self.lock`."""

    part_id: str
    part_path: str
    project_root: str | None
    hub: Hub

    model_bytes: bytes | None = None
    model_format: str = "glb"  # "glb" | "stl"
    version: int = 0
    build_ok: bool = False
    build_error: str | None = None
    node_names: list[str] = field(default_factory=list)
    bbox: dict | None = None
    volume: float | None = None

    loaded: bool = False
    building: bool = False
    last_obj: object | None = None  # cached LoadResult.obj for get_current_render
    history: list = field(default_factory=list)  # past builds for visual diff

    # ---- tabs / recents (shared across tablets + visible to Claude) ------
    open: bool = False           # shown as an open tab on the tablet
    opened_at: float = 0.0       # when it became a tab (stable tab order)
    last_active_at: float = 0.0  # last time it was selected (recents order)

    # ---- parameters (cad_viewer.params) ----------------------------------
    param_schema: list = field(default_factory=list)   # declared by the part
    param_values: dict = field(default_factory=dict)   # current overrides

    # ---- build queue bookkeeping (see builder.BuildQueue) ----------------
    build_requested_at: float = 0.0   # when it was enqueued while not building
    build_started_at: float = 0.0     # when the worker actually started it
    build_attempts: int = 0           # bumped after every finished build (ok or fail)
    # Separate Condition (its own lock) so a loader can wait for a build to
    # finish without contending on `self.lock`.
    build_cv: threading.Condition = field(default_factory=threading.Condition)

    feedback_queue: list[Feedback] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    _ids: "itertools.count[int]" = field(default_factory=lambda: itertools.count(1))

    # ---- model -----------------------------------------------------------
    def set_model(
        self,
        data: bytes,
        fmt: str,
        *,
        obj: object | None,
        node_names: list[str],
        bbox: dict | None,
        volume: float | None,
        param_schema: list | None = None,
    ) -> int:
        with self.lock:
            # keep the outgoing build for visual diff (cap 5, RAM only)
            if self.model_bytes is not None:
                self.history.append({
                    "version": self.version,
                    "format": self.model_format,
                    "data": self.model_bytes,
                })
                if len(self.history) > 5:
                    self.history = self.history[-5:]
            self.model_bytes = data
            self.model_format = fmt
            self.version += 1
            self.build_ok = True
            self.build_error = None
            self.node_names = node_names
            self.bbox = bbox
            self.volume = volume
            self.last_obj = obj
            self.loaded = True
            if param_schema is not None:
                self.param_schema = param_schema
                # drop overrides that no longer correspond to a declared param
                names = {p.get("name") for p in param_schema}
                self.param_values = {
                    k: v for k, v in self.param_values.items() if k in names
                }
            v = self.version
        log.info(
            "[%s] model v%d (%s, %d bytes, %d nodes)",
            self.part_id, v, fmt, len(data), len(node_names),
        )
        self.hub.publish(
            self.part_id,
            {"type": "reload", "version": v, "format": fmt, "part": self.part_id},
        )
        return v

    def set_build_error(self, error: str) -> None:
        with self.lock:
            self.build_ok = False
            self.build_error = error
            self.loaded = True  # it has been attempted; tablet can show the error
        log.warning("[%s] build failed:\n%s", self.part_id, error)
        self.hub.publish(
            self.part_id,
            {"type": "build_error", "version": self.version, "part": self.part_id},
        )

    # ---- build lifecycle (driven by builder.BuildQueue + watcher) --------
    def mark_build_pending(self) -> None:
        """Enqueued for a build: reflect it in the status NOW (honest spinner)."""
        with self.lock:
            if not self.building:
                self.build_requested_at = time.time()
            self.building = True
        self.hub.publish(
            self.part_id,
            {"type": "build_start", "version": self.version, "part": self.part_id},
        )

    def mark_build_done(self) -> None:
        """A build finished (ok or fail): clear `building`, wake any waiters."""
        with self.build_cv:
            with self.lock:
                self.building = False
            self.build_attempts += 1
            self.build_cv.notify_all()

    def _build_elapsed_s(self) -> float | None:
        if not self.building:
            return None
        ref = self.build_started_at or self.build_requested_at
        return round(time.time() - ref, 1) if ref else None

    def status(self) -> dict:
        with self.lock:
            return {
                "part_id": self.part_id,
                "part_path": self.part_path,
                "project_root": self.project_root,
                "version": self.version,
                "format": self.model_format,
                "loaded": self.loaded,
                "building": self.building,
                "build_elapsed_s": self._build_elapsed_s(),
                "open": self.open,
                "opened_at": self.opened_at,
                "last_active_at": self.last_active_at,
                "build_ok": self.build_ok,
                "build_error": self.build_error,
                "node_names": self.node_names,
                "bbox": self.bbox,
                "volume": self.volume,
                "param_schema": self.param_schema,
                "param_values": self.param_values,
                "has_model": self.model_bytes is not None,
                "pending_feedback": sum(
                    1 for f in self.feedback_queue if not f.consumed
                ),
            }

    # ---- feedback --------------------------------------------------------
    def add_feedback(
        self,
        png: bytes,
        note: str,
        picked_node: str | None,
        kind: str,
        picked_nodes: list[str] | None = None,
    ) -> Feedback:
        nodes = list(picked_nodes) if picked_nodes else ([picked_node] if picked_node else [])
        with self.lock:
            fb = Feedback(
                id=next(self._ids),
                created_at=time.time(),
                png=png,
                note=note or "",
                picked_node=nodes[0] if nodes else None,
                picked_nodes=nodes,
                model_version=self.version,
                kind=kind or "annotation",
                part_id=self.part_id,
            )
            self.feedback_queue.append(fb)
            if len(self.feedback_queue) > 50:
                self.feedback_queue = (
                    [f for f in self.feedback_queue if not f.consumed][-50:]
                    or self.feedback_queue[-50:]
                )
        log.info(
            "[%s] feedback #%d (%d bytes, note=%s)",
            self.part_id, fb.id, len(png), bool(note),
        )
        return fb

    # ---- version history (visual diff) ----------------------------------
    def model_at(self, version: int) -> tuple[bytes, str] | None:
        with self.lock:
            if version == self.version and self.model_bytes is not None:
                return self.model_bytes, self.model_format
            for h in self.history:
                if h["version"] == version:
                    return h["data"], h["format"]
        return None

    def history_versions(self) -> list[int]:
        with self.lock:
            return [h["version"] for h in self.history]

    def take_oldest_unconsumed(self, consume: bool) -> Feedback | None:
        with self.lock:
            for fb in self.feedback_queue:
                if not fb.consumed:
                    if consume:
                        fb.consumed = True
                    return fb
            return None

    def list_feedback(self) -> list[dict]:
        with self.lock:
            return [f.summary() for f in self.feedback_queue]


def root_of(ps: PartState) -> str:
    """The watch/rebuild key for a part: its project root, else its dir."""
    return ps.project_root or str(Path(ps.part_path).parent)


class Registry:
    """All registered parts + the shared hub, build lock and session map."""

    def __init__(self) -> None:
        self.parts: dict[str, PartState] = {}
        self.lock = threading.Lock()          # guards .parts / ._watchers mutation
        self.build_lock = threading.Lock()    # GLOBAL: serialize every build
        self.hub = Hub()
        self._watchers: dict[str, object] = {}     # root -> RootWatcher
        self._session_part: dict[str, str] = {}    # mcp-session-id -> part_id
        # Single background worker that runs every build one-at-a-time, by
        # priority, so callers enqueue instead of holding `build_lock`.
        from .builder import BuildQueue

        self.builder = BuildQueue(self)
        # Disk-persisted, per-part reference-image library (shared with Claude).
        from .references import ReferenceStore

        self.references = ReferenceStore()
        # Disk-persisted, per-part parameter overrides.
        from .paramstore import ParamStore

        self.params = ParamStore()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.hub.bind_loop(loop)

    def register_part(
        self, part_id: str, part_path: str, project_root: str | None
    ) -> PartState:
        with self.lock:
            if part_id in self.parts:
                return self.parts[part_id]
            ps = PartState(
                part_id=part_id,
                part_path=str(Path(part_path).resolve()),
                project_root=str(Path(project_root).resolve())
                if project_root
                else None,
                hub=self.hub,
            )
            ps.param_values = self.params.load(part_id)  # restore saved overrides
            self.parts[part_id] = ps
            return ps

    def resolve(self, ref: str | None) -> PartState | None:
        """Resolve a part by id OR by absolute/relative .py path."""
        if not ref:
            return None
        with self.lock:
            if ref in self.parts:
                return self.parts[ref]
            try:
                rp = str(Path(ref).resolve())
            except OSError:
                return None
            for ps in self.parts.values():
                if ps.part_path == rp:
                    return ps
        return None

    def single(self) -> PartState | None:
        with self.lock:
            return next(iter(self.parts.values())) if len(self.parts) == 1 else None

    # ---- tabs / recents --------------------------------------------------
    def touch_active(self, part_id: str) -> bool:
        """Mark a part as the active tab (opens it if needed). Returns ok."""
        ps = self.parts.get(part_id)
        if ps is None:
            return False
        now = time.time()
        with ps.lock:
            if not ps.open:
                ps.open = True
                ps.opened_at = now
            ps.last_active_at = now
        return True

    def set_open(self, part_id: str, value: bool) -> bool:
        ps = self.parts.get(part_id)
        if ps is None:
            return False
        now = time.time()
        with ps.lock:
            if value and not ps.open:
                ps.open = True
                ps.opened_at = now
                ps.last_active_at = now
            elif not value:
                ps.open = False
                ps.opened_at = 0.0  # reopening gets a fresh tab position
        return True

    def loaded_parts_under(self, root: str) -> list[PartState]:
        with self.lock:
            return [
                ps for ps in self.parts.values() if ps.loaded and root_of(ps) == root
            ]

    def summaries(self) -> list[dict]:
        with self.lock:
            parts = list(self.parts.values())
        return [
            {
                "part_id": ps.part_id,
                "part_path": ps.part_path,
                "loaded": ps.loaded,
                "building": ps.building,
                "build_elapsed_s": ps._build_elapsed_s(),
                "open": ps.open,
                "opened_at": ps.opened_at,
                "last_active_at": ps.last_active_at,
                "build_ok": ps.build_ok,
                "version": ps.version,
                "format": ps.model_format,
                "pending_feedback": sum(
                    1 for f in ps.feedback_queue if not f.consumed
                ),
            }
            for ps in parts
        ]

"""Declare tunable parameters in a build123d part — rendered as UI controls.

A part declares parameters by *calling* these helpers at module top level:

    from cad_viewer import params

    LENGTH = params.num("length", 950, min=800, max=1200, step=10)
    SPLIT  = params.flag("split", False)
    MODE   = params.choice("mode", "mono", ["mono", "split"])

    result = build_hull(LENGTH, SPLIT, MODE)

Each call (a) records a schema entry the tablet renders as a slider / checkbox /
dropdown, and (b) returns the current value — the user's override if set, else
the default. The cad-viewer server injects the overrides before importing the
part and reads back the schema after, so the SAME file drives both the default
build and every parametric variant. No more one-.py-per-variant.

The "current build" context is process-global and reset per build; the server
serializes every build under one lock, so there is never more than one build
populating it at a time (a thread-local guards against surprises anyway).
"""

from __future__ import annotations

import threading

_state = threading.local()


def _ctx() -> dict:
    c = getattr(_state, "ctx", None)
    if c is None:
        c = {"overrides": {}, "schema": []}
        _state.ctx = c
    return c


# ---- server-side hooks (called by loader.load_part) ---------------------
def _begin(overrides: dict | None) -> None:
    _state.ctx = {"overrides": dict(overrides or {}), "schema": []}


def _end() -> list[dict]:
    c = _ctx()
    schema = c["schema"]
    _state.ctx = {"overrides": {}, "schema": []}
    return schema


# ---- part-facing declarations -------------------------------------------
def num(name, default, *, min=None, max=None, step=None, label=None):
    """A numeric parameter → slider. Returns int if `default` is int, else float."""
    c = _ctx()
    c["schema"].append({
        "name": name, "type": "num", "default": default,
        "min": min, "max": max, "step": step, "label": label or name,
    })
    v = c["overrides"].get(name, default)
    try:
        if isinstance(default, bool):
            return bool(v)
        if isinstance(default, int):
            return int(round(float(v)))
        if isinstance(default, float):
            return float(v)
    except (TypeError, ValueError):
        return default
    return v


def flag(name, default=False, *, label=None):
    """A boolean parameter → checkbox."""
    c = _ctx()
    c["schema"].append({
        "name": name, "type": "bool", "default": bool(default), "label": label or name,
    })
    return bool(c["overrides"].get(name, default))


def choice(name, default, options, *, label=None):
    """A discrete parameter → dropdown. Override must be one of `options`."""
    c = _ctx()
    opts = [str(o) for o in options]
    c["schema"].append({
        "name": name, "type": "enum", "default": default,
        "options": opts, "label": label or name,
    })
    v = c["overrides"].get(name, default)
    return v if v in opts else default

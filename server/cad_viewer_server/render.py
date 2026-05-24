"""Headless PNG render of a build123d part — no GPU, no browser.

A compact vendored version of the matplotlib (Agg) iso renderer from
`my_fleet/cad/scripts/preview_iso.py` + `preview_png.py` (leaf tessellation,
label colouring, painter's depth sort). Used by the `get_current_render` MCP
tool so Claude can see the result of its own edit without the user annotating.
"""

from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")  # before pyplot — headless, deterministic
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # noqa: E402

# Same intent as preview_png.COLORS — colour leaves by label prefix.
_COLORS = {
    "PVC": "#e8e8ea", "3DP": "#ff8a2a", "UBolt": "#3a3a3a",
    "HexNut": "#4a4a4a", "HexBolt": "#4a4a4a", "Washer": "#6a6a6a",
    "WingNut": "#222222", "StarKnob": "#1f1f1f", "Thruster": "#2b6cb0",
    "Marker": "#e53e3e", "Servo": "#38a169", "Tunnel": "#a0aec0",
}


def _iter_leaves(node):
    children = getattr(node, "children", None)
    if children:
        for c in children:
            yield from _iter_leaves(c)
    else:
        yield node


def _color_for(label: str) -> str:
    for prefix, c in _COLORS.items():
        if label.startswith(prefix):
            return c
    return "#bcc3cc"


def _tessellate_all(obj, tol: float):
    items, all_xyz = [], []
    for part in _iter_leaves(obj):
        try:
            verts, tris = part.tessellate(tol)
        except Exception:  # noqa: BLE001 — STEP solids may lack triangulation
            continue
        if not verts or not tris:
            continue
        pts = np.array([(v.X, v.Y, v.Z) for v in verts])
        all_xyz.append(pts)
        polys = [[pts[i] for i in tri] for tri in tris]
        items.append((polys, _color_for(getattr(part, "label", "") or "")))
    return items, (np.vstack(all_xyz) if all_xyz else np.zeros((0, 3)))


def _depth_sort(items, view):
    elev, azim = np.radians(view[0]), np.radians(view[1])
    cam = np.array([
        np.cos(elev) * np.cos(azim),
        np.cos(elev) * np.sin(azim),
        np.sin(elev),
    ])

    def depth(polys):
        pts = np.array([p for poly in polys for p in poly])
        return pts.mean(axis=0) @ cam

    return sorted(items, key=lambda it: depth(it[0]))


def render_iso_png(obj, tol: float = 1.5, view=(25, -55)) -> bytes:
    """Return a clean single iso-view PNG of the part as bytes."""
    from .loader import quiet_stdout

    with quiet_stdout():
        items, pts = _tessellate_all(obj, tol)
    if not items:
        raise RuntimeError("Nothing to render — tessellation returned no geometry")
    mins, maxs = pts.min(0), pts.max(0)

    fig = plt.figure(figsize=(9, 9), dpi=130)
    ax = fig.add_subplot(111, projection="3d")
    ax.computed_zorder = False
    for i, (polys, c) in enumerate(_depth_sort(items, view)):
        col = Poly3DCollection(
            polys, facecolor=c, edgecolor=(0, 0, 0, 0.25), linewidth=0.2, alpha=1.0
        )
        col.set_zorder(i)
        ax.add_collection3d(col)

    pad = (maxs - mins) * 0.05 + 5.0
    ax.set_xlim(mins[0] - pad[0], maxs[0] + pad[0])
    ax.set_ylim(mins[1] - pad[1], maxs[1] + pad[1])
    ax.set_zlim(mins[2] - pad[2], maxs[2] + pad[2])
    ax.set_box_aspect((maxs - mins) + 2 * pad)
    ax.view_init(elev=view[0], azim=view[1])
    ax.set_axis_off()
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()

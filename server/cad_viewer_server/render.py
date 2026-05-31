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


# Named camera angles (elev, azim) for agent-facing multi-view renders.
_VIEWS = {
    "iso": (25, -55),
    "front": (0, -90),   # looking down -Y → the XZ face
    "side": (0, 0),      # looking down +X → the YZ face
    "top": (89, -90),    # looking down -Z → the XY plane
}


def _draw_into(ax, items, mins, maxs, view, *, dims: bool):
    ax.computed_zorder = False
    for i, (polys, c) in enumerate(_depth_sort(items, view)):
        col = Poly3DCollection(
            polys, facecolor=c, edgecolor=(0, 0, 0, 0.25), linewidth=0.2, alpha=1.0
        )
        col.set_zorder(i)
        ax.add_collection3d(col)

    size = maxs - mins
    pad = size * 0.06 + 5.0
    ax.set_xlim(mins[0] - pad[0], maxs[0] + pad[0])
    ax.set_ylim(mins[1] - pad[1], maxs[1] + pad[1])
    ax.set_zlim(mins[2] - pad[2], maxs[2] + pad[2])
    ax.set_box_aspect(size + 2 * pad)
    ax.view_init(elev=view[0], azim=view[1])

    if dims:
        # bounding box edges + per-axis size labels (mm) so scale is explicit
        x0, y0, z0 = mins
        x1, y1, z1 = maxs
        for (xa, ya, za), (xb, yb, zb) in [
            ((x0, y0, z0), (x1, y0, z0)),  # X edge
            ((x0, y0, z0), (x0, y1, z0)),  # Y edge
            ((x0, y0, z0), (x0, y0, z1)),  # Z edge
        ]:
            ax.plot([xa, xb], [ya, yb], [za, zb], color="#0a84ff", lw=1.2, alpha=0.9)
        ax.text((x0 + x1) / 2, y0 - pad[1], z0, f"X {size[0]:.0f}", color="#0a6", fontsize=11, ha="center")
        ax.text(x0 - pad[0], (y0 + y1) / 2, z0, f"Y {size[1]:.0f}", color="#0a6", fontsize=11, ha="center")
        ax.text(x0 - pad[0], y0, (z0 + z1) / 2, f"Z {size[2]:.0f}", color="#0a6", fontsize=11, ha="center")
        ax.set_xlabel("X"); ax.set_ylabel("Y"); ax.set_zlabel("Z")
        ax.tick_params(labelsize=6)
    else:
        ax.set_axis_off()
    ax.set_facecolor("white")


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
    _draw_into(ax, items, mins, maxs, view, dims=False)
    fig.patch.set_facecolor("white")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def render_views_png(obj, views=("iso", "front", "side", "top"), tol: float = 1.5,
                     dims: bool = True) -> bytes:
    """One PNG with several labelled angles + axes/size annotations — gives an
    LLM agent enough to understand shape AND scale from a single image."""
    from .loader import quiet_stdout

    with quiet_stdout():
        items, pts = _tessellate_all(obj, tol)
    if not items:
        raise RuntimeError("Nothing to render — tessellation returned no geometry")
    mins, maxs = pts.min(0), pts.max(0)

    names = [v for v in views if v in _VIEWS] or ["iso"]
    n = len(names)
    cols = 2 if n > 1 else 1
    rows = (n + cols - 1) // cols
    fig = plt.figure(figsize=(6.5 * cols, 6.0 * rows), dpi=120)
    for i, name in enumerate(names):
        ax = fig.add_subplot(rows, cols, i + 1, projection="3d")
        _draw_into(ax, items, mins, maxs, _VIEWS[name], dims=dims)
        ax.set_title(name, fontsize=13, color="#222", pad=2)
    fig.patch.set_facecolor("white")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, facecolor="white")
    plt.close(fig)
    return buf.getvalue()

"""Run a build123d part file and turn its result into web bytes.

Reproduces, exactly, the de-facto contract from
`my_fleet/cad/scripts/check.py::load_part`:

  * dynamic `importlib` of the .py file
  * the project root is inserted on `sys.path` so `from fleet_cad import params`
    (and any sibling package import) resolves — WITHOUT this, real parts fail
    with ImportError while a trivial test_cube would deceptively pass
  * a top-level `result` / `part` / `model` that is a build123d `Shape`

Difference vs check.py: the project root is passed explicitly (`--project-root`)
because this server does not live inside the target project's `scripts/` dir
and so cannot derive it from `__file__`.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import sys
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path


@contextlib.contextmanager
def quiet_stdout():
    """Send anything build123d/OCP writes to stdout → stderr instead.

    The MCP stdio transport owns the real stdout (captured by the SDK at
    startup); build123d is chatty ("BuildPart context requested by ...").
    A single stray byte on stdout breaks the JSON-RPC framing.
    """
    with contextlib.redirect_stdout(sys.stderr):
        yield

from build123d import Compound, Part, Shape, export_gltf, export_stl

_PART_VARS = ("result", "part", "model")


@dataclass
class LoadResult:
    obj: Shape
    model_bytes: bytes
    model_format: str  # "glb" | "stl"
    node_names: list[str]
    bbox: dict | None
    volume: float | None


class PartError(Exception):
    """Carries a human-readable traceback for Claude / the tablet."""


def _ensure_sys_path(project_root: Path | None) -> None:
    if project_root is None:
        return
    root = str(project_root.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)


def _import_part(file_path: Path, project_root: Path | None):
    _ensure_sys_path(project_root)
    spec = importlib.util.spec_from_file_location("_cad_viewer_part", file_path)
    if spec is None or spec.loader is None:
        raise PartError(f"Cannot create import spec for {file_path}")
    module = importlib.util.module_from_spec(spec)
    # Drop a previous instance so a re-run re-executes top-level code fresh.
    sys.modules.pop("_cad_viewer_part", None)
    sys.modules["_cad_viewer_part"] = module
    # ALSO clear cached sibling modules in the SAME directory tree as the
    # file being loaded + any sys.modules entry that SHADOWS a sibling .py
    # file (name collision : two parts/<vehicle>/<part>/hull.py both register
    # as `hull` in sys.modules). Without this, shell.py does `from hull import
    # ...` and gets a hull.py from a *different* vehicle (loaded earlier),
    # silently — symptoms : TypeError on signature mismatch, or stale results.
    # Scope LIMITED to sibling dir to keep heavy shared helpers (fleet_cad/*)
    # cached → 5-10x faster live editing. Trade-off : fleet_cad/* changes need
    # a container restart to propagate.
    file_dir = file_path.resolve().parent
    file_dir_str = str(file_dir)
    sibling_stems = {p.stem for p in file_dir.glob("*.py")}
    stale_keys = [
        name for name, mod in list(sys.modules.items())
        if name not in {"_cad_viewer_part", "__main__"}
        and (
            # Same directory tree
            (getattr(mod, "__file__", None)
             and str(Path(mod.__file__).resolve().parent) == file_dir_str)
            # OR name shadow : sibling .py exists with this module name
            or name in sibling_stems
        )
    ]
    for name in stale_keys:
        sys.modules.pop(name, None)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 — surface the full traceback
        raise PartError(
            f"Error executing {file_path.name}: {type(exc).__name__}: {exc}\n\n"
            + traceback.format_exc()
        ) from exc
    return module


def _extract_shape(module, file_path: Path) -> tuple[str, Shape]:
    for name in _PART_VARS:
        if hasattr(module, name):
            obj = getattr(module, name)
            if isinstance(obj, Shape):
                return name, obj
            raise PartError(
                f"`{name}` in {file_path.name} is not a build123d Shape: "
                f"got {type(obj).__name__}"
            )
    raise PartError(
        f"No `result`, `part`, or `model` variable found in {file_path.name}"
    )


def _iter_leaves(node):
    children = getattr(node, "children", None)
    if children:
        for c in children:
            yield from _iter_leaves(c)
    else:
        yield node


def _node_names(obj: Shape) -> list[str]:
    names: list[str] = []
    for leaf in _iter_leaves(obj):
        label = getattr(leaf, "label", "") or ""
        if label and label not in names:
            names.append(label)
    return names


def _geometry_stats(obj: Shape) -> tuple[dict | None, float | None]:
    bbox = None
    volume = None
    try:
        bb = obj.bounding_box()
        bbox = {
            "size": [round(bb.size.X, 3), round(bb.size.Y, 3), round(bb.size.Z, 3)],
            "min": [round(bb.min.X, 3), round(bb.min.Y, 3), round(bb.min.Z, 3)],
            "max": [round(bb.max.X, 3), round(bb.max.Y, 3), round(bb.max.Z, 3)],
        }
    except Exception:  # noqa: BLE001
        pass
    if isinstance(obj, (Part, Compound)):
        try:
            volume = round(float(obj.volume), 3)
        except Exception:  # noqa: BLE001
            pass
    return bbox, volume


def _to_web_bytes(obj: Shape) -> tuple[bytes, str]:
    """GLB first (colour + node names), STL as a guaranteed fallback."""
    with tempfile.TemporaryDirectory() as td:
        glb = Path(td) / "model.glb"
        try:
            ok = export_gltf(obj, str(glb), binary=True)
            if ok and glb.exists() and glb.stat().st_size > 0:
                return glb.read_bytes(), "glb"
        except Exception:  # noqa: BLE001 — fall through to STL
            pass
        stl = Path(td) / "model.stl"
        if not export_stl(obj, str(stl)) or not stl.exists():
            raise PartError("Both export_gltf and export_stl failed for this part")
        return stl.read_bytes(), "stl"


def load_part(file_path: Path, project_root: Path | None) -> LoadResult:
    """Import the part file, return geometry + web-ready bytes. Raises PartError."""
    if not file_path.exists():
        raise PartError(f"Part file does not exist: {file_path}")
    with quiet_stdout():
        module = _import_part(file_path, project_root)
        var_name, obj = _extract_shape(module, file_path)
        data, fmt = _to_web_bytes(obj)
        bbox, volume = _geometry_stats(obj)
    return LoadResult(
        obj=obj,
        model_bytes=data,
        model_format=fmt,
        node_names=_node_names(obj),
        bbox=bbox,
        volume=volume,
    )


def part_info_text(res: LoadResult) -> str:
    lines = [f"type: {type(res.obj).__name__}", f"format: {res.model_format}"]
    if res.bbox:
        s = res.bbox["size"]
        lines.append(f"bounding_box_mm: {s[0]} x {s[1]} x {s[2]}")
        lines.append(f"min: {res.bbox['min']}  max: {res.bbox['max']}")
    if res.volume is not None:
        lines.append(f"volume_mm3: {res.volume} ({res.volume / 1000:.2f} cm3)")
    if res.node_names:
        lines.append("nodes (labels): " + ", ".join(res.node_names))
    return "\n".join(lines)

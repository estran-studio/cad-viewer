"""Demo of the cad_viewer.params system — drives the ⚙️ panel on the tablet.

Edit nothing to get variants: move the sliders / toggles in the studio (or call
the set_part_params MCP tool) and the model rebuilds. One file, many variants.
"""

from build123d import (
    Axis, Box, BuildPart, Cylinder, Mode, chamfer, fillet,
)

from cad_viewer import params

W = params.num("width", 60, min=20, max=120, step=2, label="Largeur (mm)")
D = params.num("depth", 40, min=20, max=120, step=2, label="Profondeur (mm)")
H = params.num("height", 30, min=10, max=100, step=2, label="Hauteur (mm)")
HOLE = params.flag("hole", True, label="Trou central")
# default "raw" keeps the first build instant; fillet/chamfer are OCP-heavy
# (noticeably slow under Rosetta/amd64 in Docker).
EDGE = params.choice("edge", "raw", ["raw", "fillet", "chamfer"], label="Arêtes")

with BuildPart() as demo:
    Box(W, D, H)
    if HOLE:
        Cylinder(radius=min(W, D) * 0.18, height=H + 2, mode=Mode.SUBTRACT)
    try:
        vertical = demo.edges().filter_by(Axis.Z)
        if EDGE == "fillet":
            fillet(vertical, radius=min(W, D) * 0.08)
        elif EDGE == "chamfer":
            chamfer(vertical, length=min(W, D) * 0.06)
    except Exception:  # noqa: BLE001 — keep extreme param combos buildable
        pass

result = demo.part

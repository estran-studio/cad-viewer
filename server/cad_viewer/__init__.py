"""Part-facing helpers, importable from any build123d part file.

Shipped inside the cad-viewer server wheel so `from cad_viewer import params`
resolves for every registered part (the loader puts the project root on
sys.path, and this package is installed in the server venv).
"""

from . import params  # noqa: F401

__all__ = ["params"]

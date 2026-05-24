"""cad-viewer-server — one process, two faces.

- FastMCP over stdio  → Claude reads annotations / rebuilds / sees its result.
- FastAPI over HTTP/WS → the tablet renders the live model and draws on it.

stdout is reserved for MCP JSON-RPC framing. Everything logs to stderr.
"""

__version__ = "0.1.0"

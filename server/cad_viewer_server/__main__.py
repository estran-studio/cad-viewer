"""Entry point — ONE long-lived process.

Serves the studio + /api + /ws + FastMCP (Streamable HTTP) at /mcp on a single
uvicorn app. Multiple Claude Code instances register the same HTTP URL and
share this process's Registry. Started by hand and left running.

Logs go to stderr (stdout no longer carries the MCP protocol — HTTP does).
`--stdio` keeps the legacy single-part stdio MCP as a migration fallback.
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
from pathlib import Path

import uvicorn

from .config import Config, load_config
from .state import Registry


def _get_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _setup_logging() -> None:
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("[cad-viewer] %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(logging.INFO)


_UVICORN_LOG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"d": {"format": "[uvicorn] %(levelname)s %(message)s"}},
    "handlers": {"e": {"class": "logging.StreamHandler",
                       "stream": "ext://sys.stderr", "formatter": "d"}},
    "root": {"level": "WARNING", "handlers": ["e"]},
}


def main() -> int:
    ap = argparse.ArgumentParser(prog="cad-viewer-serve", description=__doc__)
    ap.add_argument("--config", type=Path, default=None,
                    help="cad-viewer.toml (default: ./cad-viewer.toml if present)")
    ap.add_argument("--part", type=Path, default=None,
                    help="ad-hoc single part .py (adds one part to the registry)")
    ap.add_argument("--project-root", type=Path, default=None,
                    help="sys.path root for the ad-hoc --part")
    ap.add_argument("--host", default=None, help="override [server].host")
    ap.add_argument("--http-port", type=int, default=None,
                    help="override [server].port")
    ap.add_argument("--stdio", action="store_true",
                    help="legacy: serve the single registered part over stdio MCP")
    args = ap.parse_args()

    _setup_logging()
    log = logging.getLogger("cad-viewer")

    cfg_path = args.config
    if cfg_path is None and Path("cad-viewer.toml").is_file():
        cfg_path = Path("cad-viewer.toml")
    cfg = load_config(cfg_path) if cfg_path else Config()

    registry = Registry()
    for pid, abs_part, abs_root in cfg.parts:
        if not Path(abs_part).exists():
            log.warning("config part missing, skipped: %s", abs_part)
            continue
        registry.register_part(pid, abs_part, abs_root)

    if args.part:
        p = args.part.resolve()
        if not p.exists():
            log.error("ad-hoc part not found: %s", p)
            return 1
        r = args.project_root.resolve() if args.project_root else None
        registry.register_part(f"adhoc/{p.stem}", str(p), str(r) if r else None)

    if not registry.parts:
        log.warning("no parts registered (empty config and no --part)")

    host = args.host or cfg.host
    port = args.http_port or cfg.port

    if args.stdio:
        # Legacy fallback: stdio MCP over the (single) registered part.
        from .mcp_app import build_mcp

        log.info("cad-viewer — legacy stdio MCP (%d part[s])", len(registry.parts))
        build_mcp(registry).run(transport="stdio")
        return 0

    ip = _get_local_ip()
    log.info("cad-viewer — HTTP + MCP on %s:%d", host, port)
    log.info("  tablette : http://%s:%d", ip, port)
    log.info("  MCP      : http://localhost:%d/mcp", port)
    for pid, _, _ in cfg.parts:
        log.info("  pièce    : %s", pid)

    from .http_app import create_app

    uvicorn.run(
        create_app(registry),
        host=host,
        port=port,
        log_config=_UVICORN_LOG,
        access_log=False,
        ws="websockets",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

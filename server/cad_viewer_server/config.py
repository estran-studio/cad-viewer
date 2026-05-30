"""cad-viewer.toml loader (tomllib — stdlib on 3.13, zero dependency).

    [server]
    host = "0.0.0.0"     # optional (tablet needs LAN; MCP clients use localhost)
    port = 32325         # optional

    [[project]]
    name = "my_fleet"
    root = "/abs/path/to/my_fleet/cad"          # inserted on sys.path (loader)
    parts = ["parts/boat/thruster_mount/thruster_mount.py", ...]   # relpaths
    glob  = "parts/**/*.py"                      # OPTIONAL, unioned with parts

part_id = "<project name>/<relpath without .py>", collisions get a -N suffix.
"""

from __future__ import annotations

import fnmatch
import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("cad-viewer")


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 32325
    # (part_id, abs_part_path, abs_project_root|None)
    parts: list[tuple[str, str, str | None]] = field(default_factory=list)


def load_config(path: Path) -> Config:
    data = tomllib.loads(Path(path).read_text())
    server = data.get("server", {})
    cfg = Config(
        host=server.get("host", "0.0.0.0"),
        port=int(server.get("port", 32325)),
    )

    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for proj in data.get("project", []):
        name = proj["name"]
        root = str(Path(proj["root"]).expanduser().resolve())
        rels = list(proj.get("parts", []))
        # glob discovery, minus any `exclude` fnmatch patterns (the explicit
        # `parts` list is intentional and never excluded). Lets one project own
        # a subtree while another hides part of it (e.g. consolidated helpers).
        excludes = list(proj.get("exclude", []))
        if proj.get("glob"):
            for p in sorted(Path(root).glob(proj["glob"])):
                if p.name == "__init__.py" or not p.is_file():
                    continue
                rel = p.relative_to(root).as_posix()
                if any(fnmatch.fnmatch(rel, pat) for pat in excludes):
                    continue
                if rel not in rels:
                    rels.append(rel)

        for rel in rels:
            abs_part = str((Path(root) / rel).resolve())
            if abs_part in seen_paths:  # explicit ∪ glob overlap → no dup
                continue
            seen_paths.add(abs_part)
            base = f"{name}/{Path(rel).with_suffix('').as_posix()}"
            pid = base
            i = 2
            while pid in seen_ids:
                pid = f"{base}-{i}"
                i += 1
            seen_ids.add(pid)
            cfg.parts.append((pid, abs_part, root))

    log.info("config: %d part(s) from %s", len(cfg.parts), path)
    return cfg

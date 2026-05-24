# cad-viewer — build123d ⇄ Claude loop (multi-part, multi-Claude)

`@bascanada/cad-viewer` is a Svelte 5 + three.js custom-element lib. Two
elements ship in one bundle (`dist/components.js`):

- `<cad-viewer>` — embeddable renderer (used by `vancad`, `vscode-openscad`).
  Public API unchanged: props `payload`, `payloadType` (`stl|3mf|glb`),
  `viewerBackgroundColor`, `gizmoScale`, `persistenceId`. **Do not break this.**
- `<cad-studio>` — the tablet page. Sidebar of all registered parts, switch
  instantly (per-part camera via `persistenceId`), Freeze & Draw, paste a
  reference image, Send → Claude.

`server/` is a separate Python (`uv`) project: **one long-lived process** =
FastAPI studio + `/api/*` + `/ws` + **FastMCP over Streamable HTTP at `/mcp/`**,
all on one port. N Claude Code instances connect to the same URL and share one
`Registry`. Builds are lazy + cached; one filesystem watcher per project root.

## Run (start once, leave running)

```bash
cd /Users/wq/Project/bascanada/cad-viewer && pnpm install && npm run build   # once
make serve     # = uv run --project server cad-viewer-serve --config ./cad-viewer.toml
```

Parts come from `cad-viewer.toml` (`[[project]]` root + explicit `parts=[…]`).
Tablet: `http://<mac-LAN-or-tailscale-ip>:32325`. MCP: `http://localhost:32325/mcp/`.

## MCP registration (already in ~/.claude.json → mcpServers)

```json
"cad-viewer": { "type": "http", "url": "http://localhost:32325/mcp/" }
```

The standalone server must be running **before** Claude Code starts. Every
Claude instance uses this same entry (not spawned per client). The trailing
slash on `/mcp/` is required.

## The loop — IMPORTANT for Claude

You may run alongside other Claude instances, each on a different part.

1. **Bind to the part you're editing**: call `select_part` with its `part_id`
   (or pass `part="<id or abs .py path>"` on every tool call — explicit always
   wins). `list_parts` shows every registered part + its state.
2. Edit the build123d `.py`. The watcher rebuilds → the tablet (if on that
   part) auto-reloads. `rebuild_part` forces it; the traceback comes back on
   failure.
3. **When the user says "regarde mes annotations" / "check my annotations"**,
   `list_feedback` (cheap) then `get_annotated_feedback` — scoped to *your*
   part. Fix the `.py`. Repeat.
4. `get_current_render` = headless iso PNG of your part (see your own edit
   without the user annotating).

Tools: `list_parts`, `select_part`, `open_part`, `list_feedback`,
`get_annotated_feedback(consume=True)`, `get_current_render`,
`rebuild_part`, `get_part_info` — all part-scoped (`part=` arg or session bind).

## Notes

- `--stdio` flag = legacy single-part stdio MCP fallback (one process per
  client, not shared) if HTTP MCP is unavailable.
- stdout is no longer the MCP channel (HTTP carries it); logs go to stderr.
- Backups of `~/.claude.json`: `~/.claude.json.bak-cadviewer*`.

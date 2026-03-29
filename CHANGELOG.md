# Changelog

## [0.1.9] - 2026-03-29
### Add-on
- **Switch to new standalone GitHub Copilot CLI.**
  The deprecated `gh-copilot` extension is replaced by the new `copilot` binary
  installed via `curl -fsSL https://gh.io/copilot-install | PREFIX=/usr/local bash`.
  The Dockerfile no longer runs `gh extension install`.
- **Simplified sidebar UI.**
  Removed the explicit auth gate. The `copilot` CLI handles its own authentication
  via the `/login` slash command typed directly in the terminal. No browser redirect
  or device-code card in the HA sidebar.
- **Cleaner PTY spawn.**
  The WebSocket terminal now spawns bare `copilot` (no arguments).  Users get the
  full interactive TUI: animated banner, slash commands, model picker, MCP agent.
- **GH_TOKEN forwarded automatically.**
  If `gh auth login` has stored a token it is forwarded to `copilot` as `GH_TOKEN`
  so the Copilot CLI can use it without requiring a manual `/login`.
- **`/chat` non-interactive fallback** (for the HA conversation agent) attempts
  `copilot --no-tty` with stdin piped; falls back gracefully with a clear error
  message directing users to authenticate in the sidebar.

## [0.1.4] - 2026-03-29
### Add-on
- **New: GitHub Copilot chat panel (ingress)**
  The add-on now serves a full chat UI at its HTTP port.  With `"ingress": true`
  in config.json, HA Supervisor creates a **Copilot** entry in the sidebar — no
  external URL or port-forwarding needed.
- **New: GitHub authentication built in.**
  The chat UI walks you through the GitHub device-flow sign-in (the same flow
  used by the VS Code Copilot extension).  Your OAuth token is persisted to
  `/data/github_token.json` and survives add-on restarts.
- **New API endpoints:** `/chat`, `/auth/device`, `/auth/status`, `/auth/poll`,
  `/auth/revoke`.
- **New files:** `auth.py` (device-flow + Copilot token exchange),
  `copilot.py` (Copilot chat completions API client),
  `static/index.html` (responsive dark-theme chat UI).

### Integration
- **New: HA conversation agent.**
  After set-up the integration registers a Copilot conversation agent so it
  appears in **Settings → Voice Assistants**.  Any HA conversation input
  (text, voice, developer tools) is forwarded to Copilot and the reply is
  returned inline.
- `manifest.json`: added `"dependencies": ["conversation"]`.
- `__init__.py`: calls `conversation.async_set_agent()` on load and
  `async_unset_agent()` on unload.

## [0.1.3] - 2026-03-29
- **Rebuilt from ha-basic-addon patterns.**
  Every file now follows ha-basic-addon v0.1.13 as the source of truth:
  - `Dockerfile`: identical pattern (COPY run.sh main.py requirements.txt,
    pip install + chmod in single RUN, EXPOSE, ENTRYPOINT).
  - `addon/run.sh`: identical (`#!/usr/bin/env bash`, `set -euo pipefail`,
    `python3 main.py`).
  - `addon/main.py`: same structure (load_options → register_discovery →
    ThreadingHTTPServer) with `ha_mcp_bridge` service name and port 8099.
  - Integration (`const`, `helpers`, `__init__`, `coordinator`, `sensor`,
    `config_flow`, `strings`, `translations`): all mirror ha-basic-addon
    patterns exactly, with MCP-specific additions on top.
- **New: `async_step_mcp_setup` config flow step.**
  After Supervisor discovery confirmation (or manual setup), users see a
  form to optionally provide an MCP server URL and bearer token. Leaving
  the URL blank skips MCP monitoring. The coordinator probes the URL on
  every poll and the `MCP Server` sensor reports `connected` /
  `unreachable` / `not_configured`.
- **Sensors**: `Status`, `Uptime` (matching ha-basic-addon), plus `MCP Server`.


### Changed — add-on
- **Rebuilt add-on from ha-basic-addon foundation.**
  `addon/main.py` now uses the exact same s6-overlay-safe structure as
  `ha-basic-addon`: no `asyncio`, no MCP probing in the add-on process.
  The add-on is responsible for *one* thing: register Supervisor discovery
  and serve `/health` + `/status`. This eliminates the s6-overlay startup
  failure caused by the previous complex entry point.
- `Dockerfile` mirrors `ha-basic-addon` exactly (`COPY requirements.txt`
  before `main.py`, `EXPOSE 8099`).

### Added — integration
- **MCP server connection step in config flow.**
  After the Supervisor discovery confirmation, users see a new `mcp_setup`
  form where they can optionally provide an MCP server URL and bearer token.
  The coordinator validates reachability at setup time and on every poll.
  Leaving the URL blank skips MCP monitoring (safe default).
- `McpAvailableSensor` — new sensor exposing `connected` / `unreachable` /
  `not_configured` based on the coordinator's MCP probe result.
- Options flow updated: users can change `mcp_url` and `mcp_token` after
  setup without re-running the full config flow.

### Changed — integration
- Coordinator now polls add-on `/health` (not `/status`) as the primary
  liveness check. MCP probing is done independently by the coordinator using
  the URL stored in the config entry — not via the add-on process.
- `BridgeStatusSensor` retained; `McpToolCountSensor` replaced by
  `McpAvailableSensor` (simpler, more reliable).

## [0.1.1] - 2026-03-29
- **Fix (critical): s6-overlay Dockerfile.**
  HA base images use s6-overlay as PID 1 (`ENTRYPOINT ["/init"]`). The previous
  `Dockerfile` had `CMD ["/app/run.sh"]` which overrides the base image entrypoint,
  bypassing s6-overlay entirely → `s6-overlay-suexec: fatal: can only run as pid 1`.
  Fix: removed `CMD`; service script is now registered at
  `/etc/services.d/ha-mcp-bridge/run` so s6-overlay starts and supervises it.
- **Fix: `run.sh` shebang changed to `#!/usr/bin/with-contenv bashio`.**
  `with-contenv` reads from s6-overlay's container environment store and exports
  variables (including `SUPERVISOR_TOKEN`) before exec-ing the process. Without it
  `SUPERVISOR_TOKEN` is empty and both discovery registration and MCP probing silently
  fail with "no_supervisor_token".

## [0.1.0] - 2026-03-29
- Initial release.
- Supervisor add-on (`addon/main.py`):
  - Calls `POST http://supervisor/discovery` on startup to trigger HA config flow.
  - Probes HA's built-in MCP Server at `/core/api/mcp_server/sse` via Supervisor proxy.
  - Exposes `/health` and `/status` HTTP endpoints for the integration to poll.
- Custom integration (`custom_components/ha_mcp_bridge`):
  - Two-step Supervisor discovery: `async_step_hassio` (validate) → `async_step_hassio_confirm` (`_set_confirm_only()` → badge).
  - Manual setup via `async_step_user`.
  - `DataUpdateCoordinator` polls `/status` every 60 s (configurable via options flow).
  - `sensor.*_status` — add-on health status.
  - `sensor.*_mcp_tool_count` — number of MCP tools from HA's MCP Server.
- `translations/en.json` for runtime UI strings.
- GitHub Actions: version consistency check + auto GitHub Release on push.

# Changelog

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

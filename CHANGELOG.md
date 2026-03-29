# Changelog

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

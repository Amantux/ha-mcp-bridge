# HA MCP Bridge

A Home Assistant **Supervisor add-on** + **custom integration** that connects to
Home Assistant's built-in [MCP Server](https://www.home-assistant.io/integrations/mcp_server/)
and surfaces MCP tool availability as discoverable HA entities.

---

## Add to Home Assistant

### Step 1 — Add the Supervisor repository

[![Add repository to Home Assistant Supervisor](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FAmantux%2Fha-mcp-bridge)

Or manually: **Settings → Add-ons → Add-on Store → ⋮ → Repositories** → paste `https://github.com/Amantux/ha-mcp-bridge`

### Step 2 — Add the integration via HACS

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Amantux&repository=ha-mcp-bridge&category=integration)

Or manually in HACS: **Integrations → ⋮ → Custom repositories** → paste `https://github.com/Amantux/ha-mcp-bridge` → category **Integration**.

### Step 3 — Start the add-on, watch it appear automatically

Install **HA MCP Bridge** from the add-on store, then **Start** it.
A **New device found** card appears in **Settings → Devices & Services** automatically.

[![Start config flow](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=ha_mcp_bridge)

---

## How it works

```
┌──────────────────────────────────────────────────────────────────────┐
│  Supervisor host                                                      │
│                                                                       │
│  ┌──────────────────────────┐        ┌───────────────────────────┐   │
│  │  HA MCP Bridge add-on    │        │  Home Assistant core      │   │
│  │  (Docker container)      │   1    │                           │   │
│  │  main.py starts          │──POST─▶│  Supervisor /discovery    │   │
│  │  register_discovery()    │        │  validates + assigns UUID │   │
│  │                          │        │           │               │   │
│  │  probe_mcp_server()   2  │◀──────▶│  /core/api/mcp_server/sse │   │
│  │  lists MCP tools         │        │  (HA's built-in MCP Srv)  │   │
│  │                          │        │           │ 3             │   │
│  │  ThreadingHTTPServer     │        │           ▼               │   │
│  │  GET /health             │        │  async_step_hassio()      │   │
│  │  GET /status  ◀──────────│────────│  validate /health    4    │   │
│  │                          │        │           │               │   │
│  └──────────────────────────┘        │           ▼               │   │
│                                      │  async_step_hassio_       │   │
│                                      │    confirm()              │   │
│                                      │  _set_confirm_only()  5   │   │
│                                      │  → "New device found"     │   │
│                                      │     badge in UI           │   │
│                                      │           │ user clicks   │   │
│                                      │           ▼               │   │
│                                      │  coordinator polls        │   │
│                                      │  /status every 60 s       │   │
└──────────────────────────────────────┴───────────────────────────┘   
```

| # | What | Why |
|---|------|-----|
| **1** | `register_discovery()` POSTs to `http://supervisor/discovery` | `"discovery"` in `config.json` is only an allowlist — the add-on must make the call or HA never fires `async_step_hassio`. |
| **2** | Add-on probes `/core/api/mcp_server/sse` via Supervisor proxy | No user token needed — `SUPERVISOR_TOKEN` is injected automatically. Checks if HA's MCP Server is enabled and lists available tools. |
| **3** | Supervisor forwards `HassioServiceInfo(uuid, config)` to HA core | `context={"source": SOURCE_HASSIO}` → HA routes to `async_step_hassio` (not `async_step_discovery`, which is mDNS/DHCP only). |
| **4** | `async_step_hassio` validates `/health` before showing UI | Fail fast. If add-on isn't ready, abort cleanly with no orphaned notification. |
| **5** | `async_step_hassio_confirm` calls `_set_confirm_only()` | This is the call that surfaces the **"New device found"** badge. Without it the flow completes silently with no visible notification. |

---

## Entities

| Entity | State | Notes |
|--------|-------|-------|
| `sensor.*_status` | `ok` / error string | Add-on liveness |
| `sensor.*_mcp_tool_count` | integer | Tools exposed by HA's MCP Server; `0` if MCP Server not enabled; attributes include tool names and error |

---

## Pre-requisite: enable HA's MCP Server

For `mcp_tool_count` to show a non-zero value, enable the built-in integration:

**Settings → Devices & Services → Add integration → search "MCP Server"** → install.

---

## Options

**Settings → Devices & Services → HA MCP Bridge → Configure**

| Option | Default | Range |
|--------|---------|-------|
| Poll interval | 60 s | 10 – 3600 s |

---

## Development

```bash
# Test add-on locally (no Supervisor token = discovery skipped, MCP probe skipped)
python3 addon/main.py
curl http://127.0.0.1:8099/health
curl http://127.0.0.1:8099/status

# Version bump checklist (both files must match):
# 1. config.json          "version": "x.y.z"
# 2. manifest.json        "version": "x.y.z"
# 3. CHANGELOG.md         new entry
# 4. git commit && git push
# → CI validates version sync, auto-creates GitHub Release for HACS
```

See [CHANGELOG.md](CHANGELOG.md) for full history.

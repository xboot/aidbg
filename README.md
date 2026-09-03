# aidbg

A collection of lightweight debugging tools, each exposing its own MCP server
so an AI can help you debug live hardware/systems over the Model Context
Protocol.

Each tool is a standalone process: you start it, and the AI connects to its MCP
endpoint as a client. The AI does **not** start anything by itself.

## Layout

```
aidbg/
├── xserial/          # serial-port interactive terminal + MCP server
├── xrelay/           # 4-channel USB relay board MCP server
├── xcamera/          # camera MCP server (local preview + photo/frame capture)
└── ...               # more tools coming
```

Each subdirectory is self-contained (own `README.md`, own dependencies).

## MCP endpoints

| Server   | Description                                  | Default endpoint               |
|----------|----------------------------------------------|--------------------------------|
| `xserial`| Shared serial session + interactive terminal | `http://127.0.0.1:30000/mcp`   |
| `xrelay` | 4-channel USB relay board (auto-finds its node) | `http://127.0.0.1:30001/mcp` |
| `xcamera`| Camera capture (photo + frame sequences)     | `http://127.0.0.1:30002/mcp` (preview `http://127.0.0.1:30003/`) |

Every server binds `0.0.0.0` by default, so it is reachable both locally and
from other machines on your LAN. From another machine, replace `127.0.0.1`
with this host's LAN IP, e.g. `http://192.168.x.x:30002/mcp`. Start any server
with `--host 127.0.0.1` to restrict it to this machine. There is **no auth**,
so only expose these on networks you trust.

## Connect an AI (opencode.json)

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "xserial": {
      "type": "remote",
      "url": "http://127.0.0.1:30000/mcp",
      "enabled": true
    },
    "xrelay": {
      "type": "remote",
      "url": "http://127.0.0.1:30001/mcp",
      "enabled": true
    },
    "xcamera": {
      "type": "remote",
      "url": "http://127.0.0.1:30002/mcp",
      "enabled": true
    }
  }
}
```

If a service isn't running, opencode can't fetch its tools. Have the AI call
that server's status tool first (e.g. `serial_info`) to confirm it's up; if it
fails, tell the AI to ask you to start the server.

## Getting started

Pick a tool and follow its own README, e.g.:

```bash
cd aidbg
python3 -m venv .venv
.venv/bin/pip install mcp pyserial
.venv/bin/python xserial/xserial.py /dev/ttyUSB0 115200
```

Relay board (auto-finds the USB node; `--device` pins it):

```bash
.venv/bin/python xrelay/xrelay.py
```

Camera debugging (needs `ffmpeg` on PATH). Start it, then open the local
preview to aim the camera at your board:

```bash
.venv/bin/pip install mcp
.venv/bin/python xcamera/xcamera.py    # preview: http://127.0.0.1:30003/
```

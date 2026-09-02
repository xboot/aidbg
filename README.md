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
├── xcamera/          # camera MCP server (photo + frame sequence capture)
├── xrelay/           # 4-channel USB relay board MCP server
└── ...               # more tools coming
```

Each subdirectory is self-contained (own `README.md`, own dependencies).

## MCP endpoints

| Server   | Description                                  | Default endpoint               |
|----------|----------------------------------------------|--------------------------------|
| `xserial`| Shared serial session + interactive terminal | `http://127.0.0.1:30000/mcp`   |
| `xcamera`| Camera capture (photo + frame sequences)     | `http://127.0.0.1:30001/mcp`   |
| `xrelay` | 4-channel USB relay board (auto-finds its node) | `http://127.0.0.1:30002/mcp` |

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
    "xcamera": {
      "type": "remote",
      "url": "http://127.0.0.1:30001/mcp",
      "enabled": true
    },
    "xrelay": {
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

Camera debugging (needs `ffmpeg` on PATH):

```bash
.venv/bin/pip install mcp
.venv/bin/python xcamera/xcamera.py
```

Relay board (auto-finds the USB node; `--device` pins it):

```bash
.venv/bin/python xrelay/xrelay.py
```

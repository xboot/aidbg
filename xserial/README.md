# xserial — serial interactive terminal + MCP server

A single file, `xserial.py`: one process owns the physical serial port. You
interact with the device directly in the terminal (like minicom), while the
same process also serves MCP over Streamable-HTTP, so an AI can connect to
`http://127.0.0.1:30000/mcp` and share the exact same serial session with you.

**The AI does not start this program.** You start it; the AI is just a client.

## Install

```bash
cd aidbg
python3 -m venv .venv
.venv/bin/pip install mcp pyserial
```

## Run

```bash
.venv/bin/python xserial/xserial.py /dev/ttyUSB0 115200
```

Arguments:

- `baud`    baudrate (default `115200`)
- `--http`  MCP HTTP port (default `30000`); the AI connects to `http://127.0.0.1:30000/mcp`
- `--host`  bind address (default `0.0.0.0` = reachable via LAN IP; use `127.0.0.1` for this machine only)

Press **Ctrl-]** in the terminal to exit (the serial port closes with it).

## Connect an AI (opencode.json)

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "xserial": {
      "type": "remote",
      "url": "http://127.0.0.1:30000/mcp",
      "enabled": true
    }
  }
}
```

If the service isn't running, opencode can't fetch its tools. Have the AI call
`serial_info` first to confirm the session is up; if it fails, it will ask you
to start `xserial.py`. If the AI runs on another machine, point `url` at this
host's LAN IP instead of `127.0.0.1`.

## Provided tools

| Tool                  | Purpose                                                        |
|-----------------------|----------------------------------------------------------------|
| `serial_info`         | Show session status (port/baudrate/buffer/prompt + MCP URL)     |
| `serial_read`         | Passive read (logs, boot output)                               |
| `serial_send_command` | Send one CLI command; collect response on "timeout + prompt" (`prompt` / `newline` / `timeout`) |
| `serial_write_bytes`  | Send arbitrary raw bytes / binary frames (supports `\n` `\xHH` escapes) |

## Typical debug flow

```
AI: serial_info
AI: serial_send_command("help")
AI: serial_send_command("mem read 0x20000000", prompt=">")
```

You see all device output in your own terminal at the same time, and can type
commands directly too.

## Serial permissions

On Linux, add your user to the `dialout` group to avoid `sudo`:

```bash
sudo usermod -aG dialout $USER
```

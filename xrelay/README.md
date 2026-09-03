# xrelay — 4-channel USB relay board + MCP server

A single file, `xrelay.py`: controls a 4-channel USB relay module (9600 8-N-1)
and serves MCP over Streamable-HTTP, so an AI can connect to
`http://127.0.0.1:30001/mcp` and switch relays while you debug hardware
(power-cycle a board, cut/restore a supply line, drive load switches, ...).

**The AI does not start this program.** You start it; the AI is just a client.

## Wire protocol

One 4-byte frame per action (`checksum = (0xA0 + addr + cmd) & 0xFF`):

```
TX: A0 <addr 01-04> <cmd> <checksum>
    cmd: 00 off(no ack)   01 on(no ack)   02 off(ack)   03 on(ack)
         04 toggle(ack, not used here)   05 query state(ack)
RX: A0 <addr> <00|01> <checksum>       (ack frames: 00=off, 01=on)
```

Examples: `A0 01 01 A2` = ch1 on, `A0 03 05 A8` = query ch3.

## Node changes between replugs? Two options

- **Auto-find (default):** run `xrelay.py` with no `--device`. On the first
  command it probes `/dev/ttyUSB*` / `/dev/ttyACM*` with a read-only state
  query (cmd 05 — it does **not** actuate relays) and caches the node that
  answers. Ports already held open by another process (e.g. xserial) are
  skipped. If the board later re-enumerates under a different node, the next
  command fails once and re-scans automatically.
- **Pin it:** `xrelay.py --device /dev/ttyUSB1` to skip probing entirely.

## Install

```bash
cd aidbg
python3 -m venv .venv
.venv/bin/pip install mcp pyserial
```

## Run

```bash
.venv/bin/python xrelay/xrelay.py                    # auto-find the board
.venv/bin/python xrelay/xrelay.py --device /dev/ttyUSB1
```

Arguments:

- `--device`  pin the serial node (default: auto-find by probing)
- `--http`    MCP HTTP port (default `30001`); the AI connects to `http://127.0.0.1:30001/mcp`
- `--host`    bind address (default `0.0.0.0` = reachable via LAN IP; use `127.0.0.1` for this machine only)

Press **Ctrl-C** to exit.

## Connect an AI (opencode.json)

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "xrelay": {
      "type": "remote",
      "url": "http://127.0.0.1:30001/mcp",
      "enabled": true
    }
  }
}
```

If the service isn't running, opencode can't fetch its tools. Have the AI call
`relay_info` first to confirm the server is up; if it fails, it will ask you
to start `xrelay.py`. If the AI runs on another machine, point `url` at this
host's LAN IP instead of `127.0.0.1`.

## Provided tools

| Tool            | Purpose                                                                 |
|-----------------|-------------------------------------------------------------------------|
| `relay_info`    | Session status: node in use, candidates, last known states (no bus traffic) |
| `relay_status`  | Live state query, channel 1-4 or all (`cmd 05`, read-only)               |
| `relay_control` | `on` / `off` one channel; ack frame verified by default                  |
| `relay_pulse`   | Momentary: on for `duration_ms` (50-10000, default 500), then off        |

`relay_control` uses the ack commands (02/03) by default and checks the reply,
so the answer reflects the real switch state. `wait_feedback=False` sends the
fire-and-forget frames (00/01) instead. `relay_pulse` holds the serial lock
for the whole on -> sleep -> off sequence, so nothing interleaves mid-pulse;
the firmware's cmd 04 (toggle) is not used — flip a channel by querying with
`relay_status` and setting the opposite with `relay_control`.

## Typical debug flow

```
AI: relay_info
AI: relay_status(channel=0)          # all four, live
AI: relay_control(channel=2, action="on")
AI: relay_control(channel=2, action="off")   # power-cycled the DUT
AI: relay_pulse(channel=2, duration_ms=200)  # momentary blip, e.g. a reset line
```

## Serial permissions

On Linux, add your user to the `dialout` group to avoid `sudo`:

```bash
sudo usermod -aG dialout $USER
```

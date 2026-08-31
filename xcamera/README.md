# xcamera - camera MCP server for hardware debugging

A single file, `xcamera.py`: one process owns the camera (via `ffmpeg`/V4L2) and
serves MCP over Streamable-HTTP on `http://127.0.0.1:30001/mcp`, so an AI can
take photos and record short frame sequences while debugging live hardware --
reading a board's display, LEDs, or the oscilloscope screen next to the serial
console.

**The AI does not start this program.** You start it; the AI is just a client.

Uses `ffmpeg` as the capture backend (no OpenCV dependency). A single photo or
a frame sequence is captured per call; concurrent tool calls are serialized so
they never fight over `/dev/videoN`.

## Install

```bash
cd aidbg
python3 -m venv .venv
.venv/bin/pip install mcp
sudo apt install ffmpeg   # capture backend
```

## Run

```bash
.venv/bin/python xcamera/xcamera.py
```

Arguments:

- `--device`  V4L2 device (default `/dev/video0`)
- `--http`    MCP HTTP port (default `30001`); the AI connects to `http://127.0.0.1:30001/mcp`

Press **Ctrl-C** to exit.

## Connect an AI (opencode.json)

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "xcamera": {
      "type": "remote",
      "url": "http://127.0.0.1:30001/mcp",
      "enabled": true
    }
  }
}
```

If the service isn't running, opencode can't fetch its tools. Have the AI call
`camera_info` first to confirm the session is up; if it fails, it will ask you
to start `xcamera.py`.

## Provided tools

| Tool                  | Purpose                                                                |
|-----------------------|------------------------------------------------------------------------|
| `camera_info`         | Show session status (device / ffmpeg / MCP URL)                        |
| `camera_take_photo`   | Capture one JPEG photo (`width` / `height` / `quality`)                |
| `camera_record_video` | Record JPEG frames over a duration (`duration_seconds` 1-60 / `max_frames` / ...) |

Note: `quality` is the ffmpeg `-q:v` scale (2 = best, 31 = worst), not a
percentage.

## Typical debug flow

```
AI: camera_info
AI: camera_take_photo()                  # read what's on the scope/screen now
AI: camera_record_video(duration_seconds=5, max_frames=10)   # catch a blinking LED
```

Combine with `xserial`: the AI talks to the device over serial while watching
its screen/LEDs through the camera.

## Troubleshooting

- **ffmpeg not found** -- install it (`sudo apt install ffmpeg`).
- **Device or resource busy** -- another program (or another `xcamera.py`)
  holds the camera; close it first.
- **Resolution not supported** -- ffmpeg falls back to the nearest supported
  size; check `v4l2-ctl --list-formats-ext` (or `ffmpeg -f v4l2 -list_formats
  all -i /dev/video0`) for supported sizes.

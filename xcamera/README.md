# xcamera - camera MCP server for hardware debugging

A single file, `xcamera.py`: one process owns the camera (via `ffmpeg`/V4L2)
and runs two servers side by side:

- a **local preview** at `http://127.0.0.1:30003/` -- open it in a browser to
  aim the camera at your board/screen while you work;
- a **remote MCP server** at `http://127.0.0.1:30002/mcp` -- so an AI can take
  photos and record short frame sequences while debugging live hardware
  (reading a board's display, LEDs, or the oscilloscope screen next to the
  serial console).

**The AI does not start this program.** You start it; the AI is just a client.

Architecture: one persistent `ffmpeg` capture streams MJPEG into a shared
frame buffer (latest frame + a 10 fps ring covering the last 60 s). Preview
and MCP tools read from that buffer -- no re-encoding, no per-call camera
open, and concurrent tool calls never fight over `/dev/videoN`. If the device
is unplugged or `ffmpeg` dies, the capture restarts automatically.

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

- `--device`       V4L2 device (default `/dev/video0`)
- `--width/--height/--framerate` capture mode (default `1280x720@30`);
  must be a mode your camera supports in MJPEG (check
  `v4l2-ctl --list-formats-ext`), e.g. `--width 1920 --height 1080`
- `--http`         MCP HTTP port (default `30002`)
- `--preview-port` local preview port (default `30003`)
- `--host`         bind address (default `0.0.0.0` = reachable via LAN IP;
  use `127.0.0.1` for this machine only)
- `--no-preview`   skip the preview server

Press **Ctrl-C** to exit.

## Local preview

Open `http://127.0.0.1:30003/` in a browser (MJPEG `<img>` stream); from
another machine on the LAN use `http://<host-ip>:30003/` instead:

- `/`            live view page
- `/stream`      raw MJPEG stream (also works in VLC / other viewers)
- `/snapshot.jpg` one current frame

Memory note: the ring buffer keeps ~10 fps of compressed JPEG for 60 s
(~70 MB at 1280x720, ~150 MB at 1920x1080).

## Connect an AI (opencode.json)

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "xcamera": {
      "type": "remote",
      "url": "http://127.0.0.1:30002/mcp",
      "enabled": true
    }
  }
}
```

If the service isn't running, opencode can't fetch its tools. Have the AI call
`camera_info` first to confirm the session is up; if it fails, it will ask you
to start `xcamera.py`. If the AI runs on another machine, point `url` at this
host's LAN IP instead of `127.0.0.1`.

## Provided tools

| Tool                  | Purpose                                                                |
|-----------------------|------------------------------------------------------------------------|
| `camera_info`         | Show session status (device / stream health / URLs / last error)       |
| `camera_take_photo`   | Grab the freshest frame from the live stream                           |
| `camera_record_video` | Record frames over a duration (`duration_seconds` 1-60 / `max_frames`, sampled <=10 fps) |

Photo resolution is the server capture mode (`--width/--height`), not a
per-call argument.

## Typical debug flow

```
you: open http://127.0.0.1:30003/ and aim the camera at the board
AI:  camera_info
AI:  camera_take_photo()                  # read what's on the scope/screen now
AI:  camera_record_video(duration_seconds=5, max_frames=10)   # catch a blinking LED
```

Combine with `xserial`: the AI talks to the device over serial while watching
its screen/LEDs through the camera.

## Troubleshooting

- **ffmpeg not found** -- install it (`sudo apt install ffmpeg`).
- **Device or resource busy** -- another program holds the camera; close it.
- **Unsupported resolution / stream DOWN** -- pick a mode from
  `v4l2-ctl --list-formats-ext` and restart with `--width/--height/--framerate`;
  `camera_info` shows `last_error` with ffmpeg's complaint.

#!/usr/bin/env python3
"""xcamera.py -- camera MCP server for hardware debugging

One process owns the camera: a persistent ffmpeg/V4L2 reader streams MJPEG
frames into a shared buffer that feeds both a local browser preview and the
remote MCP tools, so while debugging live hardware you can aim the camera at
the board/screen yourself and the AI takes photos and records short frame
sequences (reading a board's display, LEDs, or oscilloscope screen next to
the serial console).

Usage:
    xcamera.py [--device /dev/video0] [--width 1280 --height 720]
               [--http 30002] [--preview-port 30003] [--host 0.0.0.0]
               [--no-preview]

Serves on 0.0.0.0 by default, so both this machine and other machines on the
LAN can connect (use --host 127.0.0.1 to restrict to this machine).

The AI does not start this program; you do. Frames pass through as JPEG
bytes without re-encoding (ffmpeg -c copy), and concurrent tool calls share
the stream instead of fighting over /dev/videoN.

Requires: ffmpeg on PATH (no OpenCV needed).
"""

import argparse
import logging
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image

DEFAULT_HOST = "0.0.0.0"
DEFAULT_HTTP_PORT = 30002
DEFAULT_PREVIEW_PORT = 30003
HTTP_PATH = "/mcp"
DEFAULT_DEVICE = "/dev/video0"
DEFAULT_WIDTH, DEFAULT_HEIGHT, DEFAULT_FRAMERATE = 1280, 720, 30
MIN_DURATION, MAX_DURATION = 1.0, 60.0
RING_FPS = 10            # ring-buffer sampling rate (cap for recorded frames)
STALE_SECONDS = 5.0      # take_photo rejects frames older than this
SOI, EOI = b"\xff\xd8", b"\xff\xd9"

mcp = MCPServer("xcamera")

host = DEFAULT_HOST


def _lan_ip() -> str:
    """Best-effort LAN IP for display (no packet is actually sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _advertise() -> str:
    """Host shown in URLs: the bind host if specific, else the LAN IP."""
    return host if host not in ("0.0.0.0", "::") else _lan_ip()


class CameraError(Exception):
    """Raised when the camera stream or capture fails."""


def _split_mjpeg(buf: bytes) -> tuple[list[bytes], bytes]:
    """Extract complete JPEG frames (SOI..EOI) from an MJPEG byte stream."""
    frames: list[bytes] = []
    while True:
        start = buf.find(SOI)
        if start < 0:
            buf = buf[-1:]  # keep a possible partial SOI marker
            break
        end = buf.find(EOI, start + 2)
        if end < 0:
            buf = buf[start:]
            break
        frames.append(buf[start:end + 2])
        buf = buf[end + 2:]
    return frames, buf


class Camera:
    """One ffmpeg reader owns the device; frames go to `latest` + a ring buffer."""

    def __init__(self, device: str, width: int, height: int, framerate: int):
        self.device = device
        self.width = width
        self.height = height
        self.framerate = framerate
        self.lock = threading.Lock()          # guards latest/ring/streaming/error
        self.latest: Optional[bytes] = None
        self.latest_ts = 0.0
        self.streaming = False
        self.last_error = ""
        self.ring: deque[tuple[float, bytes]] = deque()
        self._seq = 0
        self._stop = threading.Event()

    @staticmethod
    def check_ffmpeg() -> None:
        if shutil.which("ffmpeg") is None:
            raise CameraError("ffmpeg not found on PATH; install ffmpeg (e.g. 'sudo apt install ffmpeg')")

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def start(self) -> None:
        threading.Thread(target=self._reader, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _spawn(self, err_file) -> subprocess.Popen:
        return subprocess.Popen(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "v4l2", "-input_format", "mjpeg",
                "-framerate", str(self.framerate),
                "-video_size", f"{self.width}x{self.height}",
                "-i", self.device,
                "-f", "mjpeg", "-c", "copy", "-",
            ],
            stdout=subprocess.PIPE,
            stderr=err_file,
            bufsize=0,
        )

    def _ingest(self, frame: bytes) -> None:
        now = time.monotonic()
        self._seq += 1
        step = max(1, round(self.framerate / RING_FPS))
        with self.lock:
            self.latest = frame
            self.latest_ts = now
            if self._seq % step == 0:
                self.ring.append((now, frame))
                horizon = now - MAX_DURATION
                while self.ring and self.ring[0][0] < horizon:
                    self.ring.popleft()

    def _reader(self) -> None:
        """Keep one ffmpeg capture running; restart on device loss or failure."""
        while not self.stopping:
            if not Path(self.device).exists():
                with self.lock:
                    self.streaming = False
                    self.last_error = f"{self.device} not present"
                print(f"[xcamera] waiting for {self.device}", file=sys.stderr)
                self._stop.wait(2.0)
                continue

            err_file = tempfile.TemporaryFile()
            proc = self._spawn(err_file)
            buf = b""
            assert proc.stdout is not None
            try:
                while not self.stopping:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    frames, buf = _split_mjpeg(buf)
                    if not frames:
                        continue
                    for f in frames:
                        self._ingest(f)
                    with self.lock:
                        was = self.streaming
                        self.streaming = True
                    if not was:
                        print(f"[xcamera] streaming {self.width}x{self.height}@{self.framerate} "
                              f"from {self.device}", file=sys.stderr)
            finally:
                proc.kill()
                proc.wait()
                err_file.seek(0)
                err = err_file.read().decode(errors="replace").strip()
                err_file.close()
            if self.stopping:
                break
            tail = err.splitlines()[-1][:300] if err else "ffmpeg exited"
            with self.lock:
                self.streaming = False
                self.last_error = tail
            print(f"[xcamera] capture stopped, retrying in 2s: {tail}", file=sys.stderr)
            self._stop.wait(2.0)

    def take_photo(self) -> bytes:
        """Return the freshest JPEG frame from the stream."""
        with self.lock:
            data, age = self.latest, time.monotonic() - self.latest_ts if self.latest else None
        if data is None or age is None or age > STALE_SECONDS:
            raise CameraError(f"no fresh frame ({self.last_error or 'camera not streaming'})")
        return data

    def capture_frames(self, duration_seconds: float, max_frames: int) -> list[bytes]:
        """Collect frames from the live stream over `duration_seconds`."""
        if not (MIN_DURATION <= duration_seconds <= MAX_DURATION):
            raise CameraError(
                f"duration must be {MIN_DURATION}-{MAX_DURATION} seconds, got {duration_seconds}"
            )
        start = time.monotonic()
        end = start + duration_seconds
        while time.monotonic() < end and not self.stopping:
            time.sleep(0.05)
        stop_ts = time.monotonic()
        with self.lock:
            frames = [data for ts, data in self.ring if start <= ts <= stop_ts]
        if not frames:
            raise CameraError("no frames captured (camera not streaming)")
        if len(frames) > max_frames:
            step = len(frames) / max_frames
            frames = [frames[int(i * step)] for i in range(max_frames)]
        return frames


camera: Optional[Camera] = None


def _camera() -> Camera:
    if camera is None:
        raise RuntimeError("camera not initialized")
    return camera


@mcp.tool()
def camera_info() -> str:
    """Show current camera session status: device, stream health, MCP and preview URLs."""
    cam = _camera()
    with cam.lock:
        streaming = cam.streaming
        age = time.monotonic() - cam.latest_ts if cam.latest else None
        err = cam.last_error
    dev_exists = Path(cam.device).exists()
    lines = [
        f"device={cam.device} ({'present' if dev_exists else 'MISSING'})",
        f"mode={cam.width}x{cam.height}@{cam.framerate} mjpeg",
        f"stream={'up' if streaming else 'DOWN'}"
        + (f", last frame {age:.1f}s ago" if age is not None else ", no frames yet"),
        f"ffmpeg={shutil.which('ffmpeg') or 'NOT FOUND'}",
        f"mcp_url=http://{_advertise()}:{http_port}{HTTP_PATH}",
        f"preview_url=http://{_advertise()}:{preview_port}/",
    ]
    if err:
        lines.append(f"last_error={err}")
    return "\n".join(lines)


@mcp.tool()
def camera_take_photo() -> Image:
    """Capture a single photo from the live stream (good for reading displays,
    oscilloscope screens, LEDs, or board labels during hardware debugging).
    Resolution is set at server start (--width/--height)."""
    return Image(data=_camera().take_photo(), format="jpeg")


@mcp.tool()
def camera_record_video(duration_seconds: float = 3.0, max_frames: int = 12) -> list[Image]:
    """Record a short frame sequence (for spotting blinks, moving traces,
    changing states that one photo would miss).

    Frames are sampled evenly (up to 10 fps) from the live stream over the
    duration and returned as JPEG images.

    Args:
        duration_seconds: how long to record, 1.0-60.0 seconds (default 3.0)
        max_frames: maximum frames returned, 1-60 (default 12)
    """
    max_frames = max(1, min(60, max_frames))
    frames = _camera().capture_frames(duration_seconds, max_frames)
    return [Image(data=f, format="jpeg") for f in frames]


http_port = DEFAULT_HTTP_PORT
preview_port = DEFAULT_PREVIEW_PORT


def _serve_mcp() -> None:
    """Serve MCP over streamable-http (blocking; runs in a background thread)."""
    logging.getLogger("mcp").setLevel(logging.ERROR)
    mcp.settings.log_level = "ERROR"
    try:
        mcp.run(
            transport="streamable-http",
            host=host,
            port=http_port,
            streamable_http_path=HTTP_PATH,
        )
    except OSError as e:
        print(f"[xcamera] MCP server failed on {host}:{http_port}: {e}", file=sys.stderr)


PREVIEW_PAGE = """<!doctype html>
<html><head><title>xcamera preview</title><meta charset="utf-8">
<style>
  body {{ margin:0; background:#111; color:#9f9; font:14px monospace;
          display:flex; flex-direction:column; align-items:center; }}
  img {{ max-width:100vw; max-height:93vh; object-fit:contain; }}
  p {{ margin:6px; }}
  a {{ color:#9f9; }}
</style></head>
<body>
<p>xcamera {width}x{height}@{framerate} &middot; {device} &middot;
   <a href="/snapshot.jpg">snapshot</a></p>
<img src="/stream" alt="waiting for frames...">
</body></html>
"""


class _PreviewHandler(BaseHTTPRequestHandler):
    """MJPEG preview: / = page, /stream = live MJPEG, /snapshot.jpg = one frame."""

    def do_GET(self):
        path = self.path.split("?")[0]
        cam = _camera()
        if path == "/":
            body = PREVIEW_PAGE.format(
                width=cam.width, height=cam.height,
                framerate=cam.framerate, device=cam.device,
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/stream":
            self._stream(cam)
        elif path == "/snapshot.jpg":
            try:
                frame = cam.take_photo()
            except CameraError as e:
                self.send_error(503, str(e))
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.end_headers()
            self.wfile.write(frame)
        else:
            self.send_error(404)

    def _stream(self, cam: Camera):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=xcamera")
        self.end_headers()
        last_ts = 0.0
        try:
            while not cam.stopping:
                with cam.lock:
                    frame, ts = cam.latest, cam.latest_ts
                if frame is None or ts == last_ts:
                    time.sleep(0.04)
                    continue
                last_ts = ts
                self.wfile.write(
                    b"--xcamera\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
                )
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


def _serve_preview(port: int) -> None:
    """Serve the local preview (blocking; runs in a background thread)."""
    try:
        ThreadingHTTPServer((host, port), _PreviewHandler).serve_forever()
    except OSError as e:
        print(f"[xcamera] preview server failed on {host}:{port}: {e}", file=sys.stderr)


def run(device: str, http: int, preview: int, no_preview: bool,
        width: int, height: int, framerate: int, bind_host: str = DEFAULT_HOST) -> None:
    global camera, http_port, preview_port, host
    http_port = http
    preview_port = preview
    host = bind_host
    Camera.check_ffmpeg()
    camera = Camera(device, width, height, framerate)
    if not Path(device).exists():
        print(f"[xcamera] warning: {device} not present yet; waiting for it", file=sys.stderr)
    camera.start()
    if not no_preview:
        threading.Thread(target=_serve_preview, args=(preview,), daemon=True).start()
        print(f"[xcamera] preview: http://{_advertise()}:{preview}/", file=sys.stderr)
    threading.Thread(target=_serve_mcp, daemon=True).start()
    print(f"[xcamera] MCP server: http://{_advertise()}:{http}{HTTP_PATH}", file=sys.stderr)
    print("[xcamera] press Ctrl-C to exit", file=sys.stderr)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    if camera is not None:
        camera.stop()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="xcamera",
        description="Camera MCP server for hardware debugging "
                    "(local preview + photo / frame sequence capture over MCP)",
        epilog="Examples:\n  xcamera.py\n"
               "  xcamera.py --device /dev/video0 --width 1920 --height 1080",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--device", default=DEFAULT_DEVICE, help=f"V4L2 device (default {DEFAULT_DEVICE})")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH, help=f"capture width (default {DEFAULT_WIDTH})")
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT, help=f"capture height (default {DEFAULT_HEIGHT})")
    parser.add_argument("--framerate", type=int, default=DEFAULT_FRAMERATE,
                        help=f"capture framerate (default {DEFAULT_FRAMERATE})")
    parser.add_argument("--http", type=int, default=DEFAULT_HTTP_PORT,
                        help=f"MCP HTTP port (default {DEFAULT_HTTP_PORT})")
    parser.add_argument("--preview-port", type=int, default=DEFAULT_PREVIEW_PORT,
                        help=f"local preview port (default {DEFAULT_PREVIEW_PORT})")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"bind address; 0.0.0.0 = reachable via LAN IP, "
                             f"127.0.0.1 = local only (default {DEFAULT_HOST})")
    parser.add_argument("--no-preview", action="store_true", help="disable the local preview server")
    args = parser.parse_args()
    run(args.device, args.http, args.preview_port, args.no_preview,
        args.width, args.height, args.framerate, args.host)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""xcamera.py -- camera MCP server for hardware debugging

One process owns the camera (via ffmpeg/V4L2): it serves MCP over
Streamable-HTTP so an AI can take photos and record short frame sequences
while debugging live hardware, e.g. reading a board's display, LEDs, or
oscilloscope screen next to the serial console.

Usage:
    xcamera.py [--device /dev/video0] [--http 30001]

The AI does not start this program; you do.

Requires: ffmpeg on PATH (no OpenCV needed). The camera is opened per capture
(same trick as xserial sharing one port): a global lock serializes captures so
concurrent tool calls don't fight over /dev/videoN.
"""

import argparse
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.types import Image

HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 30001
HTTP_PATH = "/mcp"
DEFAULT_DEVICE = "/dev/video0"
DEFAULT_INPUT_FORMAT = "mjpeg"
DEFAULT_WARMUP_FRAMES = 5
MIN_DURATION, MAX_DURATION = 1.0, 60.0

mcp = MCPServer("xcamera")


class CameraError(Exception):
    """Raised when the camera or ffmpeg fails."""


class Camera:
    """Serializes access to one V4L2 device via ffmpeg subprocesses."""

    def __init__(self, device: str):
        self.device = device
        self.lock = threading.Lock()

    @staticmethod
    def _check_ffmpeg() -> None:
        if shutil.which("ffmpeg") is None:
            raise CameraError("ffmpeg not found on PATH; install ffmpeg (e.g. 'sudo apt install ffmpeg')")

    def _run(self, args: list[str], timeout: float) -> None:
        """Run ffmpeg; raise CameraError with its stderr tail on failure."""
        try:
            proc = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error"] + args,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise CameraError(f"ffmpeg timed out after {timeout:.0f}s")
        if proc.returncode != 0:
            err = proc.stderr.decode(errors="replace").strip()
            raise CameraError(f"ffmpeg failed (rc={proc.returncode}): {err or 'no stderr'}")

    def _warmup(self) -> None:
        """Discard a few frames so auto-exposure settles; failures are ignored."""
        try:
            self._run(
                [
                    "-f", "v4l2", "-input_format", DEFAULT_INPUT_FORMAT,
                    "-framerate", "10", "-video_size", "640x480",
                    "-i", self.device,
                    "-frames:v", str(DEFAULT_WARMUP_FRAMES),
                    "-f", "null", "-",
                ],
                timeout=15.0,
            )
        except CameraError:
            pass  # warm-up is best-effort; the real capture reports errors

    def _capture(self, width: int, height: int, quality: int, out: Path) -> None:
        self._run(
            [
                "-f", "v4l2", "-input_format", DEFAULT_INPUT_FORMAT,
                "-framerate", "5",
                "-video_size", f"{width}x{height}",
                "-i", self.device,
                "-frames:v", "1", "-update", "1",
                "-q:v", str(quality),
                str(out),
            ],
            timeout=30.0,
        )

    def take_photo(self, width: int = 1920, height: int = 1080, quality: int = 5) -> bytes:
        """Capture one JPEG photo. Must be called under self.lock."""
        with tempfile.TemporaryDirectory(prefix="xcamera-") as tmp:
            out = Path(tmp) / "photo.jpg"
            self._capture(width, height, quality, out)
            try:
                return out.read_bytes()
            except FileNotFoundError:
                raise CameraError("ffmpeg produced no image (camera busy or no signal)")

    def capture_frames(
        self,
        duration_seconds: float,
        width: int = 640,
        height: int = 480,
        max_frames: int = 24,
        quality: int = 8,
    ) -> list[bytes]:
        """Record JPEG frames sampled evenly over the duration."""
        if not (MIN_DURATION <= duration_seconds <= MAX_DURATION):
            raise CameraError(
                f"duration must be {MIN_DURATION}-{MAX_DURATION} seconds, got {duration_seconds}"
            )
        fps = max(1.0, min(10.0, max_frames / duration_seconds))
        frames_dir_fps = min(fps, 25.0)
        with tempfile.TemporaryDirectory(prefix="xcamera-") as tmp:
            pattern = str(Path(tmp) / "f%05d.jpg")
            self._run(
                [
                    "-f", "v4l2", "-input_format", DEFAULT_INPUT_FORMAT,
                    "-framerate", f"{frames_dir_fps}",
                    "-video_size", f"{width}x{height}",
                    "-i", self.device,
                    "-t", f"{duration_seconds}",
                    "-vf", f"fps={fps}",
                    "-q:v", str(quality),
                    pattern,
                ],
                timeout=duration_seconds + 30.0,
            )
            files = sorted(Path(tmp).glob("f*.jpg"))
            if not files:
                raise CameraError("ffmpeg produced no frames (camera busy or no signal)")
            if len(files) > max_frames:
                step = len(files) / max_frames
                files = [files[int(i * step)] for i in range(max_frames)]
            return [f.read_bytes() for f in files]


camera: Optional[Camera] = None


def _camera() -> Camera:
    if camera is None:
        raise RuntimeError("camera not initialized")
    return camera


@mcp.tool()
def camera_info() -> str:
    """Show current camera session status and MCP server info."""
    assert camera is not None
    dev_exists = Path(camera.device).exists()
    return (
        f"device={camera.device} ({'present' if dev_exists else 'MISSING'})\n"
        f"ffmpeg={shutil.which('ffmpeg') or 'NOT FOUND'}\n"
        f"mcp_url=http://{HOST}:{http_port}{HTTP_PATH}"
    )


@mcp.tool()
def camera_take_photo(width: int = 1920, height: int = 1080, quality: int = 5) -> Image:
    """Capture a single photo from the camera (good for reading displays,
    oscilloscope screens, LEDs, or board labels during hardware debugging).

    Args:
        width: image width in pixels (default 1920)
        height: image height in pixels (default 1080)
        quality: JPEG quality 2-31, ffmpeg scale, lower is better (default 5)
    """
    cam = _camera()
    with cam.lock:
        cam._check_ffmpeg()
        cam._warmup()
        data = cam.take_photo(width=width, height=height, quality=quality)
    return Image(data=data, format="jpeg")


@mcp.tool()
def camera_record_video(
    duration_seconds: float = 3.0,
    width: int = 640,
    height: int = 480,
    max_frames: int = 12,
    quality: int = 8,
) -> list[Image]:
    """Record a short frame sequence (for spotting blinks, moving traces,
    changing states that one photo would miss).

    Frames are sampled evenly over the duration and returned as JPEG images.

    Args:
        duration_seconds: how long to record, 1.0-60.0 seconds (default 3.0)
        width: frame width in pixels (default 640)
        height: frame height in pixels (default 480)
        max_frames: maximum frames returned (default 12)
        quality: JPEG quality 2-31, ffmpeg scale, lower is better (default 8)
    """
    cam = _camera()
    with cam.lock:
        cam._check_ffmpeg()
        frames = cam.capture_frames(
            duration_seconds=duration_seconds,
            width=width,
            height=height,
            max_frames=max_frames,
            quality=quality,
        )
    return [Image(data=f, format="jpeg") for f in frames]


http_port = DEFAULT_HTTP_PORT


def _serve_http() -> None:
    """Serve MCP over streamable-http (blocking; runs in a background thread)."""
    logging.getLogger("mcp").setLevel(logging.ERROR)
    mcp.settings.log_level = "ERROR"
    try:
        mcp.run(
            transport="streamable-http",
            host=HOST,
            port=http_port,
            streamable_http_path=HTTP_PATH,
        )
    except OSError as e:
        print(f"[xcamera] MCP server failed on {HOST}:{http_port}: {e}", file=sys.stderr)


def run(device: str, http: int) -> None:
    global camera, http_port
    http_port = http
    camera = Camera(device)
    if not Path(device).exists():
        print(f"[xcamera] warning: {device} not present yet; tools will fail until it appears",
              file=sys.stderr)
    threading.Thread(target=_serve_http, daemon=True).start()
    print(f"[xcamera] camera device: {device}", file=sys.stderr)
    print(f"[xcamera] MCP server: http://{HOST}:{http}{HTTP_PATH}", file=sys.stderr)
    print("[xcamera] press Ctrl-C to exit", file=sys.stderr)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="xcamera",
        description="Camera MCP server for hardware debugging (photo + frame sequence capture)",
        epilog="Examples:\n  xcamera.py\n  xcamera.py --device /dev/video0 --http 30001",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--device", default=DEFAULT_DEVICE, help=f"V4L2 device (default {DEFAULT_DEVICE})")
    parser.add_argument("--http", type=int, default=DEFAULT_HTTP_PORT,
                        help=f"MCP HTTP port (default {DEFAULT_HTTP_PORT})")
    args = parser.parse_args()
    run(args.device, args.http)


if __name__ == "__main__":
    main()

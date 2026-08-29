#!/usr/bin/env python3
"""xserial.py -- serial interactive terminal + MCP server

One process owns the physical serial port: you interact in the terminal while it
also serves MCP over Streamable-HTTP, so an AI can share the same session via
http://127.0.0.1:30000/mcp.

Usage:
    xserial.py /dev/ttyUSB0 [baud] [--http 30000]

Press Ctrl-] in the terminal to exit (the serial port closes with it). The AI
does not start this program; you do.
"""

import argparse
import io
import logging
import os
import re
import select
import sys
import termios
import threading
import time
import tty
from typing import Callable, Optional

import serial
from mcp.server.mcpserver import MCPServer

HOST = "127.0.0.1"
DEFAULT_BAUDRATE = 115200
DEFAULT_TIMEOUT = 3.0
DEFAULT_PROMPTS = [">", "#", "$", "?"]
DEFAULT_HTTP_PORT = 30000
HTTP_PATH = "/mcp"

mcp = MCPServer("xserial")


class SerialSession:
    """Owns a single serial connection plus its background receive thread."""

    def __init__(self):
        self.ser: Optional[serial.SerialBase] = None
        self.port: Optional[str] = None
        self.http_port: int = DEFAULT_HTTP_PORT
        self.baudrate: int = 0
        self.rxbuf: bytearray = bytearray()
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.cmd_lock = threading.Lock()
        self.rx_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.observers: list[Callable[[bytes], None]] = []
        self._last_prompt = DEFAULT_PROMPTS[0]

    @property
    def is_open(self) -> bool:
        return self.ser is not None and self.ser.is_open

    @property
    def last_prompt(self) -> str:
        with self.lock:
            return self._last_prompt

    @last_prompt.setter
    def last_prompt(self, value: str) -> None:
        with self.lock:
            self._last_prompt = value

    def open(self, port: str, baudrate: int) -> None:
        self.close()
        self.ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.05,
            write_timeout=5.0,
        )
        self.port = port
        self.baudrate = baudrate
        self.rxbuf.clear()
        self._stop.clear()
        self.rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self.rx_thread.start()
        try:
            self.ser.dtr = False
            self.ser.rts = False
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        if self.rx_thread is not None:
            self._stop.set()
            self.rx_thread.join(timeout=1.0)
            self.rx_thread = None
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None
        self.port = None

    def add_observer(self, fn: Callable[[bytes], None]) -> None:
        if fn not in self.observers:
            self.observers.append(fn)

    def remove_observer(self, fn: Callable[[bytes], None]) -> None:
        if fn in self.observers:
            self.observers.remove(fn)

    def _notify(self, data: bytes) -> None:
        for fn in list(self.observers):
            try:
                fn(data)
            except Exception:
                pass

    def _rx_loop(self) -> None:
        """Background thread: read the serial port, buffer RX data, notify observers (terminal)."""
        while not self._stop.is_set():
            try:
                if self.ser is None or not self.ser.is_open:
                    time.sleep(0.05)
                    continue
                data = self.ser.read(4096)
                if data:
                    with self.lock:
                        self.rxbuf.extend(data)
                    self._notify(bytes(data))
                else:
                    time.sleep(0.02)
            except serial.SerialException:
                msg = b"\n[xserial] port disconnected\n"
                with self.lock:
                    self.rxbuf.extend(msg)
                self._notify(msg)
                break
            except Exception:
                time.sleep(0.05)

    def write(self, data: bytes) -> None:
        if not self.is_open:
            raise RuntimeError("serial port is not open")
        with self.write_lock:
            self.ser.write(data)
            self.ser.flush()

    def read_available(self) -> bytes:
        with self.lock:
            out = bytes(self.rxbuf)
            self.rxbuf.clear()
        return out

    def peek_available(self) -> bytes:
        with self.lock:
            return bytes(self.rxbuf)

    def read_until_idle(self, idle_seconds: float, max_seconds: float, terminator: Optional[str]) -> str:
        """Read the response until any of these holds:
        1. terminator configured: prompt seen and channel quiet ~0.15s (complete), or max_seconds
        2. no terminator: channel quiet for idle_seconds, or max_seconds reached
        """
        start = time.monotonic()
        last_data_time = start
        seen = b""
        term_bytes = terminator.encode(errors="ignore") if terminator else None

        while True:
            now = time.monotonic()
            chunk = self.read_available()
            if chunk:
                seen += chunk
                last_data_time = now
            if term_bytes:
                # Prompt mode: end when the prompt is at the end and quiet for a short while
                if term_bytes in seen[-len(term_bytes) - 1:] and now - last_data_time > 0.15:
                    break
                if now - last_data_time >= idle_seconds:
                    break
                if now - start >= max_seconds:
                    break
            else:
                # Pure timeout mode: end on silence or total timeout
                if now - last_data_time >= idle_seconds:
                    break
                if now - start >= max_seconds:
                    break
            time.sleep(0.02)

        if terminator:
            self.last_prompt = terminator
        return _clean_text(seen)


_ESCAPE_MAP = {"n": 10, "r": 13, "t": 9, "\\": 92}


def _unescape(text: str) -> bytes:
    """Interpret a small whitelist of escapes (\\n \\r \\t \\\\ \\xHH) in `text`.

    A literal backslash not followed by a known escape is kept as-is, so data
    like Windows paths or binary frames survive round-trips intact.
    """
    out = bytearray()
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "x" and i + 3 < n:
                try:
                    out.append(int(text[i + 2 : i + 4], 16))
                    i += 4
                    continue
                except ValueError:
                    pass
            if nxt in _ESCAPE_MAP:
                out.append(_ESCAPE_MAP[nxt])
                i += 2
                continue
            out.append(ord("\\"))
            i += 1
            continue
        out.extend(ch.encode("utf-8"))
        i += 1
    return bytes(out)


def _clean_text(text: bytes) -> str:
    """Strip ANSI escape sequences and control characters from a response."""
    ansi = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b.")
    data = ansi.sub(b"", text)
    data = re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", b"", data)
    return data.decode(errors="replace")


session = SerialSession()


class HumanTerm:
    """Interactive terminal: wire stdin/stdout directly to the serial port (raw mode). Ctrl-] exits."""

    def __init__(self, session: SerialSession):
        self.session = session
        self.out: Optional[io.BufferedWriter] = None

    def _on_rx(self, data: bytes) -> None:
        try:
            self.out.write(data)
            self.out.flush()
        except (OSError, ValueError):
            pass

    def run(self) -> None:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        self.out = sys.stdout.buffer
        print("[xserial] interactive terminal: type to send, Ctrl-] to exit\r\n", file=sys.stderr)
        self.session.add_observer(self._on_rx)
        try:
            tty.setraw(fd)
            while True:
                r, _, _ = select.select([sys.stdin], [], [], 0.1)
                if sys.stdin in r:
                    data = os.read(fd, 1024)
                    if data == b"\x1d":  # Ctrl-]
                        break
                    if not data:
                        break
                    try:
                        self.session.write(data)
                    except Exception as e:
                        self.out.write(f"\r\n[xserial] {e}\r\n".encode(errors="replace"))
                        self.out.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            self.session.remove_observer(self._on_rx)


def _require_open() -> None:
    if not session.is_open:
        raise RuntimeError("serial port is not open")


@mcp.tool()
def serial_info() -> str:
    """Show current serial session status and MCP server info."""
    if not session.is_open:
        return (
            "serial port not open\n"
            f"mcp_url=http://{HOST}:{session.http_port}{HTTP_PATH}"
        )
    return (
        f"port={session.port}\n"
        f"baudrate={session.baudrate}\n"
        f"rx_buffered={len(session.peek_available())} bytes\n"
        f"last_prompt={session.last_prompt!r}\n"
        f"mcp_url=http://{HOST}:{session.http_port}{HTTP_PATH}"
    )


@mcp.tool()
def serial_read(timeout: float = 2.0, idle: float = 0.3) -> str:
    """Passively read serial data for up to `timeout` seconds.

    Returns when the channel has been quiet for `idle` seconds after receiving
    data, or after `timeout` seconds have elapsed (hard cap). Good for
    device-pushed logs, boot output, etc.
    """
    _require_open()
    deadline = time.monotonic() + max(0.1, timeout)
    last_data_time = time.monotonic()
    seen = b""
    with session.cmd_lock:
        while True:
            chunk = session.read_available()
            now = time.monotonic()
            if chunk:
                seen += chunk
                last_data_time = now
            if now >= deadline:
                break
            if seen and now - last_data_time >= idle:
                break
            time.sleep(0.02)
    return _clean_text(seen) if seen.strip() else "(no data within timeout)"


@mcp.tool()
def serial_send_command(
    command: str,
    timeout: float = DEFAULT_TIMEOUT,
    prompt: Optional[str] = None,
    newline: Optional[str] = None,
) -> str:
    """Send a command-line command to the device and collect the response
    (returns when "timeout or prompt" is met).

    For interactive device shells (xshell/finsh/little-shell, etc.). Reads the
    response and returns when any of these holds:
      1. prompt detected and channel quiet for ~0.15s (response complete)
      2. channel quiet for more than 0.5s (most commands finished)
      3. total elapsed time reaches timeout (fallback)

    Args:
        command: command text to send
        timeout: total timeout in seconds, default 3.0
        prompt: shell prompt such as ">" or "#"; returns early when seen.
                Defaults to the last successfully used prompt
        newline: line ending "lf", "cr" or "crlf", default "lf"
    """
    _require_open()
    nl = {"cr": "\r", "crlf": "\r\n"}.get((newline or "lf").lower(), "\n")
    term = prompt if prompt else session.last_prompt
    with session.cmd_lock:
        session.read_available()  # discard stale data
        session.write((command + nl).encode(errors="replace"))
        text = session.read_until_idle(
            idle_seconds=0.5,
            max_seconds=max(0.1, timeout),
            terminator=term,
        )
    return text if text.strip() else "(no response; retry with larger timeout or check baudrate)"


@mcp.tool()
def serial_write_bytes(data: str, wait_response: bool = False, timeout: float = 2.0) -> str:
    """Send arbitrary raw bytes to the device (no auto newline, no prompt waiting).

    Escape sequences like \\n \\r \\t \\xHH in `data` are interpreted. Good for
    binary frames, special control characters, or bare-metal programs.

    Args:
        data: raw data to send (escape sequences supported)
        wait_response: whether to also read the response
        timeout: read response timeout in seconds
    """
    _require_open()
    raw = _unescape(data)
    with session.cmd_lock:
        session.write(raw)
        if not wait_response:
            return f"sent {len(raw)} bytes"
        time.sleep(0.1)
        start = time.monotonic()
        seen = b""
        while time.monotonic() - start < timeout:
            chunk = session.read_available()
            if chunk:
                seen += chunk
                start = time.monotonic()
            else:
                time.sleep(0.05)
    return _clean_text(seen) if seen.strip() else "(no response)"


def _serve_http(http_port: int) -> None:
    """Serve the MCP server over streamable-http (blocking; runs in a background thread)."""
    logging.getLogger("mcp").setLevel(logging.ERROR)  # silence SDK debug/info to keep the raw terminal clean
    mcp.settings.log_level = "ERROR"  # silence uvicorn startup/access logs that would garble the raw terminal
    try:
        mcp.run(
            transport="streamable-http",
            host=HOST,
            port=http_port,
            streamable_http_path=HTTP_PATH,
        )
    except OSError as e:
        print(f"[xserial] MCP server failed on {HOST}:{http_port}: {e}", file=sys.stderr)


def run(port: str, baudrate: int, http_port: int = DEFAULT_HTTP_PORT) -> None:
    session.http_port = http_port
    try:
        session.open(port, baudrate)
    except serial.SerialException as e:
        print(f"[xserial] Failed to open {port}: {e}", file=sys.stderr)
        sys.exit(1)
    threading.Thread(target=_serve_http, args=(http_port,), daemon=True).start()
    print(f"[xserial] Opened {port} @ {baudrate}bps", file=sys.stderr)
    print(f"[xserial] MCP server: http://{HOST}:{http_port}{HTTP_PATH}", file=sys.stderr)
    try:
        HumanTerm(session).run()
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="xserial",
        description="Serial port interactive terminal + MCP server",
        epilog="Examples:\n  xserial.py /dev/ttyUSB0 115200\nPress Ctrl-] in the terminal to exit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("port", help="serial device path (e.g. /dev/ttyUSB0, /dev/ttyACM0)")
    parser.add_argument("baud", nargs="?", type=int, default=DEFAULT_BAUDRATE, help=f"baudrate (default {DEFAULT_BAUDRATE})")
    parser.add_argument("--http", type=int, default=DEFAULT_HTTP_PORT, help=f"MCP HTTP port (default {DEFAULT_HTTP_PORT})")
    args = parser.parse_args()

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("[xserial] interactive mode requires a real terminal", file=sys.stderr)
        sys.exit(1)

    run(args.port, args.baud, http_port=args.http)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        session.close()
        sys.exit(0)

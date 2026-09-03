#!/usr/bin/env python3
"""xrelay.py -- USB relay board MCP server for hardware debugging

Controls a 4-channel USB relay board (9600 8-N-1) and serves MCP over
Streamable-HTTP, so an AI can switch relays while debugging live hardware
(power-cycle a board, cut/restore a supply line, drive load switches, ...).

Wire protocol, one 4-byte frame per action:
    TX: A0 <addr 01-04> <cmd> <checksum>    checksum = (A0+addr+cmd) & 0xFF
        cmd: 00 off(no ack)  01 on(no ack)  02 off(ack)  03 on(ack)
             04 toggle(ack, not used here)  05 query state(ack)
    RX: A0 <addr> <00|01> <checksum>        (ack frames: 00=off, 01=on)

The USB node may change between replugs (ttyUSB1 -> ttyUSB3, ...). Pin it with
--device, or run deviceless and the server auto-finds the board: candidate
ports (/dev/ttyUSB*, /dev/ttyACM*) are probed with a read-only state query
(cmd 05, does not actuate relays); ports held open by other processes are
skipped. The found node is cached and re-scanned automatically on the next
command after a replug.

Usage:
    xrelay.py [--device /dev/ttyUSB1] [--http 30001] [--host 0.0.0.0]

Serves on 0.0.0.0 by default, so both this machine and other machines on the
LAN can connect (use --host 127.0.0.1 to restrict to this machine).

The AI does not start this program; you do.

Requires: pyserial. Like xcamera, the port is opened per command and a global
lock serializes transactions, so concurrent tool calls don't fight over the
port and a replug recovers on the next command.
"""

import argparse
import glob
import logging
import os
import socket
import sys
import threading
import time
from typing import Optional

import serial
from mcp.server.mcpserver import MCPServer

DEFAULT_HOST = "0.0.0.0"
DEFAULT_HTTP_PORT = 30001
HTTP_PATH = "/mcp"
DEFAULT_BAUDRATE = 9600
FRAME_HEAD = 0xA0
CMD_OFF_NOACK = 0x00
CMD_ON_NOACK = 0x01
CMD_OFF_ACK = 0x02
CMD_ON_ACK = 0x03
CMD_QUERY = 0x05
MIN_CHANNEL, MAX_CHANNEL = 1, 4
RESP_TIMEOUT = 0.5
MIN_PULSE_MS, MAX_PULSE_MS, DEFAULT_PULSE_MS = 50, 10_000, 500

mcp = MCPServer("xrelay")

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


class RelayError(Exception):
    """Raised when the relay board cannot be reached or answers invalidly."""


def build_frame(addr: int, cmd: int) -> bytes:
    body = bytes((FRAME_HEAD, addr, cmd))
    return body + bytes((sum(body) & 0xFF,))


def parse_frame(data: bytes, addr: int) -> int:
    """Validate a 4-byte ack frame for `addr`; return the state (0 off / 1 on)."""
    if not data:
        raise RelayError("no reply from board (absent, or firmware in no-feedback mode)")
    if len(data) != 4 or data[0] != FRAME_HEAD:
        raise RelayError(f"bad reply: {data.hex(' ')}")
    if data[1] != addr:
        raise RelayError(f"reply addressed to channel {data[1]}, asked for {addr}")
    if (sum(data[:3]) & 0xFF) != data[3]:
        raise RelayError(f"bad reply checksum: {data.hex(' ')}")
    if data[2] not in (0x00, 0x01):
        raise RelayError(f"bad reply state 0x{data[2]:02X}")
    return data[2]


class RelayBoard:
    """4-channel relay board on a serial port; opens the port per transaction."""

    def __init__(self, device: Optional[str]):
        self.device = device or None      # pinned node; None => auto-find
        self.found: Optional[str] = None  # last node the board answered on
        self.lock = threading.Lock()
        self.state: dict[int, Optional[int]] = {
            ch: None for ch in range(MIN_CHANNEL, MAX_CHANNEL + 1)
        }

    # ---- node discovery -------------------------------------------------

    @staticmethod
    def candidates() -> list[str]:
        return sorted(glob.glob("/dev/ttyUSB*"))

    @staticmethod
    def _in_use(port: str) -> bool:
        """True if another process holds `port` open (skip it when probing)."""
        me = os.getpid()
        for pid in os.listdir("/proc"):
            if not pid.isdigit() or int(pid) == me:
                continue
            fd_dir = f"/proc/{pid}/fd"
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            for fd in fds:
                try:
                    if os.readlink(f"{fd_dir}/{fd}") == port:
                        return True
                except OSError:
                    continue
        return False

    def _find(self) -> str:
        """Probe candidates with a read-only state query; return the board's node."""
        notes: list[str] = []
        for port in self.candidates():
            if self._in_use(port):
                notes.append(f"{port} (busy)")
                continue
            try:
                with self._open(port) as ser:
                    ser.reset_input_buffer()
                    ser.write(build_frame(MIN_CHANNEL, CMD_QUERY))
                    reply = ser.read(4)
            except (serial.SerialException, OSError) as e:
                notes.append(f"{port} ({e})")
                continue
            if not reply:
                notes.append(f"{port} (no reply)")
                continue
            try:
                parse_frame(reply, MIN_CHANNEL)
            except RelayError:
                notes.append(f"{port} (noise: {reply.hex(' ')})")
                continue
            self.found = port
            return port
        raise RelayError(
            "relay board not found (probed: "
            + (", ".join(notes) or "no serial ports present")
            + "); pass --device to pin the node, or check power/wiring"
        )

    # ---- transactions ---------------------------------------------------

    @staticmethod
    def _open(port: str) -> serial.Serial:
        return serial.Serial(
            port=port,
            baudrate=DEFAULT_BAUDRATE,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=RESP_TIMEOUT,
            write_timeout=5.0,
        )

    def _transact_once(self, port: str, addr: int, cmd: int, expect_reply: bool) -> Optional[int]:
        with self._open(port) as ser:
            ser.reset_input_buffer()
            ser.write(build_frame(addr, cmd))
            if not expect_reply:
                ser.flush()
                time.sleep(0.05)  # let the board actuate before the port drops
                return None
            reply = ser.read(4)
        state = parse_frame(reply, addr)
        self.state[addr] = state
        return state

    def transact(self, addr: int, cmd: int, expect_reply: bool = True) -> Optional[int]:
        """Send one frame; rescan once if the board is no longer on its node."""
        with self.lock:
            port = self.device or self.found
            if port is None:
                port = self._find()
            try:
                return self._transact_once(port, addr, cmd, expect_reply)
            except (serial.SerialException, OSError, RelayError) as e:
                if self.device:
                    raise RelayError(f"{port}: {e}") from e
                self.found = None  # node changed or board unplugged: rescan
                port = self._find()
                return self._transact_once(port, addr, cmd, expect_reply)

    # ---- high-level actions ----------------------------------------------

    def status(self, addr: int) -> int:
        state = self.transact(addr, CMD_QUERY)
        return 0 if state is None else state

    def set(self, addr: int, on: bool, wait_feedback: bool = True) -> Optional[int]:
        if wait_feedback:
            state = self.transact(addr, CMD_ON_ACK if on else CMD_OFF_ACK)
            want = 1 if on else 0
            if state != want:
                raise RelayError(
                    f"channel {addr}: asked {'on' if on else 'off'}, board reports {state}"
                )
            return state
        return self.transact(addr, CMD_ON_NOACK if on else CMD_OFF_NOACK, expect_reply=False)

    def pulse(self, addr: int, duration: float, wait_feedback: bool = True) -> Optional[int]:
        """Energize for `duration` seconds then release, under one lock.

        Not built on transact(): the lock must span the whole on -> sleep ->
        off sequence or another command could cut the pulse short.
        """
        with self.lock:
            port = self.device or self.found
            if port is None:
                port = self._find()
            try:
                return self._pulse_once(port, addr, duration, wait_feedback)
            except (serial.SerialException, OSError, RelayError) as e:
                if self.device:
                    raise RelayError(f"{port}: {e}") from e
                self.found = None  # node changed or board unplugged: rescan
                port = self._find()
                return self._pulse_once(port, addr, duration, wait_feedback)

    def _pulse_once(self, port: str, addr: int, duration: float,
                    wait_feedback: bool) -> Optional[int]:
        final: Optional[int] = None
        with self._open(port) as ser:
            self._swap(ser, addr, True, wait_feedback)
            time.sleep(duration)
            final = self._swap(ser, addr, False, wait_feedback)
        if final is not None:
            self.state[addr] = final
        return final

    @staticmethod
    def _swap(ser: serial.Serial, addr: int, on: bool, wait_feedback: bool) -> Optional[int]:
        """One framed transition on an already-open port; None if no ack mode."""
        cmd = (CMD_ON_ACK if on else CMD_OFF_ACK) if wait_feedback \
            else (CMD_ON_NOACK if on else CMD_OFF_NOACK)
        ser.reset_input_buffer()
        ser.write(build_frame(addr, cmd))
        if not wait_feedback:
            ser.flush()
            time.sleep(0.05)  # let the board actuate before the next frame
            return None
        state = parse_frame(ser.read(4), addr)
        want = 1 if on else 0
        if state != want:
            raise RelayError(
                f"channel {addr}: asked {'on' if on else 'off'}, board reports {state}"
            )
        return state


board: Optional[RelayBoard] = None


def _board() -> RelayBoard:
    if board is None:
        raise RuntimeError("relay board not initialized")
    return board


def _check_channel(channel: int) -> None:
    if not (MIN_CHANNEL <= channel <= MAX_CHANNEL):
        raise ValueError(f"channel must be {MIN_CHANNEL}-{MAX_CHANNEL}, got {channel}")


@mcp.tool()
def relay_info() -> str:
    """Show relay board session status and MCP server info (does not touch the bus)."""
    b = _board()
    if b.device:
        dev = f"{b.device} (pinned, {'present' if os.path.exists(b.device) else 'MISSING'})"
    elif b.found:
        dev = f"{b.found} (auto-found last run; rescanned on next command if it fails)"
    else:
        dev = "auto-find (probes on first command)"
    states = " ".join(
        f"{ch}={'on' if s == 1 else 'off' if s == 0 else '?'}"
        for ch, s in sorted(b.state.items())
    )
    return (
        f"device={dev}\n"
        f"serial={DEFAULT_BAUDRATE} 8N1, frame=A0 <addr 01-04> <cmd> <sum>\n"
        f"last_known_state: {states} (relay_status queries live)\n"
        f"candidates={', '.join(RelayBoard.candidates()) or 'none'}\n"
        f"mcp_url=http://{_advertise()}:{http_port}{HTTP_PATH}"
    )


@mcp.tool()
def relay_status(channel: int = 0) -> str:
    """Query relay state live from the board (read-only, does not switch).

    Args:
        channel: 1-4 for one channel, or 0 for all four (default 0)
    """
    b = _board()
    if channel == 0:
        return " ".join(
            f"channel {ch}={'on' if b.status(ch) else 'off'}"
            for ch in range(MIN_CHANNEL, MAX_CHANNEL + 1)
        )
    _check_channel(channel)
    return f"channel {channel}={'on' if b.status(channel) else 'off'}"


@mcp.tool()
def relay_control(channel: int, action: str = "off", wait_feedback: bool = True) -> str:
    """Switch one relay channel on or off.

    With wait_feedback=True (default) the board's ack frame is checked, so the
    answer reflects the real switch state; with False the frame is fire-and-
    forget (use only if the board is wired/flash in no-feedback mode).

    Args:
        channel: relay channel 1-4
        action: "on" or "off"
        wait_feedback: verify via the board's ack frame (default True)
    """
    b = _board()
    _check_channel(channel)
    act = action.strip().lower()
    if act == "on":
        state = b.set(channel, True, wait_feedback=wait_feedback)
    elif act == "off":
        state = b.set(channel, False, wait_feedback=wait_feedback)
    else:
        raise ValueError(f"action must be on/off, got {action!r}")
    if state is None:
        return f"channel {channel}: {act} sent (no feedback)"
    return f"channel {channel}: {'on' if state else 'off'} (board confirmed)"


@mcp.tool()
def relay_pulse(channel: int, duration_ms: int = DEFAULT_PULSE_MS,
                wait_feedback: bool = True) -> str:
    """Momentarily energize one relay channel: on for duration_ms, then off.

    The pulse is atomic from the bus's point of view: the serial lock is held
    for the whole on -> wait -> off sequence, so other relay calls queue until
    it completes and nothing can interrupt it.

    Args:
        channel: relay channel 1-4
        duration_ms: how long to stay on, 50-10000 ms (default 500)
        wait_feedback: verify both transitions via ack frames (default True)
    """
    b = _board()
    _check_channel(channel)
    if not (MIN_PULSE_MS <= duration_ms <= MAX_PULSE_MS):
        raise ValueError(
            f"duration_ms must be {MIN_PULSE_MS}-{MAX_PULSE_MS}, got {duration_ms}"
        )
    state = b.pulse(channel, duration_ms / 1000.0, wait_feedback=wait_feedback)
    if state is None:
        return f"channel {channel}: pulsed {duration_ms} ms (no feedback, assumed off)"
    return f"channel {channel}: pulsed {duration_ms} ms, now off (board confirmed)"


http_port = DEFAULT_HTTP_PORT


def _serve_http() -> None:
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
        print(f"[xrelay] MCP server failed on {host}:{http_port}: {e}", file=sys.stderr)


def run(device: Optional[str], http: int, bind_host: str = DEFAULT_HOST) -> None:
    global board, http_port, host
    http_port = http
    host = bind_host
    board = RelayBoard(device)
    threading.Thread(target=_serve_http, daemon=True).start()
    print(f"[xrelay] device: {device or 'auto-find (probes /dev/ttyUSB*, /dev/ttyACM*) on first command'}",
          file=sys.stderr)
    print(f"[xrelay] MCP server: http://{_advertise()}:{http}{HTTP_PATH}", file=sys.stderr)
    print("[xrelay] press Ctrl-C to exit", file=sys.stderr)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="xrelay",
        description="4-channel USB relay board MCP server for hardware debugging",
        epilog="Examples:\n"
               "  xrelay.py\n"
               "  xrelay.py --device /dev/ttyUSB1 --http 30001",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--device", default=None,
                        help="pin the serial node (e.g. /dev/ttyUSB1); default: auto-find by probing")
    parser.add_argument("--http", type=int, default=DEFAULT_HTTP_PORT,
                        help=f"MCP HTTP port (default {DEFAULT_HTTP_PORT})")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"bind address; 0.0.0.0 = reachable via LAN IP, "
                             f"127.0.0.1 = local only (default {DEFAULT_HOST})")
    args = parser.parse_args()
    run(args.device, args.http, args.host)


if __name__ == "__main__":
    main()

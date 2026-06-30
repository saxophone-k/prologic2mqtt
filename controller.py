#!/usr/bin/env python3
"""
ProLogic PS4 — async connection manager.

Maintains the TCP connection to the EW11 RS-485 bridge, feeds
ProLogicParser with the raw byte stream, and fires registered
callbacks whenever the parsed state changes.

Run as a live demo:
    python controller.py [--host IP] [--port PORT] [--duration S]

Import:
    from controller import ProLogicController
    ctrl = ProLogicController("192.168.107.61", 8899)
    ctrl.register_callback(lambda state: ...)
    asyncio.run(ctrl.run())
"""

import asyncio
import logging
import sys
from datetime import datetime

from parser import ProLogicParser, format_state

_LOGGER = logging.getLogger(__name__)

HOST_DEFAULT    = "192.168.107.61"
PORT_DEFAULT    = 8899
RECONNECT_DELAY = 10   # seconds before each reconnect attempt
CONNECT_TIMEOUT = 10   # seconds for the initial TCP handshake
READ_TIMEOUT    = 5    # seconds; keepalive fires at 10 Hz so only triggers on true link loss

# ── Command frames (00 8c Aqua Pod wireless format, verified on hardware) ─────

def _mk_cmd(key4: bytes) -> bytes:
    d = bytes([0x00, 0x8c, 0x01]) + key4 + key4 + bytes([0x00])
    cs = (0x10 + 0x02 + sum(d)) & 0xFFFF
    return b"\x10\x02" + d + bytes([cs >> 8, cs & 0xFF]) + b"\x10\x03"

CMD_FILTER      = _mk_cmd(bytes.fromhex("80000000"))
CMD_JETS        = _mk_cmd(bytes.fromhex("00020000"))
CMD_POOL_SPA    = _mk_cmd(bytes.fromhex("40000000"))
CMD_HP          = _mk_cmd(bytes.fromhex("00000400"))
CMD_SUPER_CHLOR = _mk_cmd(bytes.fromhex("00000004"))


class ProLogicController:
    """
    Async TCP reader for the EW11 RS-485 bridge.

    Usage pattern:
        ctrl = ProLogicController(host, port)
        ctrl.register_callback(my_fn)   # my_fn(state: dict) called on every change
        task = asyncio.create_task(ctrl.run())
        ...
        await ctrl.stop(); task.cancel()

    State is always accessible via ctrl.state even between callbacks.
    """

    def __init__(self, host: str = HOST_DEFAULT, port: int = PORT_DEFAULT):
        self.host    = host
        self.port    = port
        self.parser  = ProLogicParser()
        self._callbacks: list = []
        self._running = False
        self._writer: asyncio.StreamWriter | None = None
        self._cmd_locks:    dict[str, asyncio.Lock] = {}
        self._cmd_debounce: dict[str, float]        = {}

    # ── public API ────────────────────────────────────────────────────────────

    @property
    def state(self) -> dict:
        """Current decoded state. Same dict object updated in place."""
        return self.parser.state

    @property
    def available(self) -> bool:
        """True once at least one valid frame has been received."""
        return self.parser.frame_count > 0

    def register_callback(self, cb) -> None:
        """Register cb(state: dict) to be called on every state change."""
        if cb not in self._callbacks:
            self._callbacks.append(cb)

    def unregister_callback(self, cb) -> None:
        if cb in self._callbacks:
            self._callbacks.remove(cb)

    async def run(self) -> None:
        """
        Connect to the EW11 and read frames indefinitely.
        Reconnects automatically after any disconnect or read error.
        Designed to run as a long-lived asyncio task.
        """
        self._running = True
        while self._running:
            try:
                await self._connect_and_read()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _LOGGER.warning(
                    "Connection to %s:%d lost (%s) — retrying in %ds",
                    self.host, self.port, exc, RECONNECT_DELAY,
                )
                if self._running:
                    await asyncio.sleep(RECONNECT_DELAY)

    async def stop(self) -> None:
        """Signal the run loop to exit cleanly."""
        self._running = False

    async def send_command(
        self,
        key: str,
        frame: bytes,
        check_fn,
        pre_check_fn=None,
        debounce: float = 20.0,
        verify_timeout: float = 20.0,
        retry_pause: float = 3.0,
    ) -> bool:
        """
        Read-before-write command sender with per-key debounce.

        key           : unique name for this control (used for debounce + lock)
        frame         : 00 8c command bytes to send
        check_fn()    : callable → True when the desired state is confirmed
        pre_check_fn(): callable → True when already at desired state (noop).
                        If None, check_fn is used for the noop check too.
        debounce      : ignore duplicate commands within this many seconds
        verify_timeout: how long to poll for confirmation after sending
        retry_pause   : seconds to wait before re-reading state / retrying

        Returns True if the desired state was (or already is) confirmed.
        """
        loop = asyncio.get_running_loop()

        # Debounce: drop commands that arrive while the previous one is still settling
        now = loop.time()
        if now - self._cmd_debounce.get(key, 0.0) < debounce:
            _LOGGER.debug("send_command %s: debounced", key)
            return True

        lock = self._cmd_locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            _LOGGER.debug("send_command %s: command in progress, dropping", key)
            return True

        async with lock:
            # Read-before-write: noop if already at desired state
            noop_check = pre_check_fn if pre_check_fn is not None else check_fn
            if noop_check():
                _LOGGER.info("send_command %s: noop (already at desired state)", key)
                return True

            # Stamp debounce before sending so rapid double-presses are dropped
            self._cmd_debounce[key] = loop.time()

            # Double-send (200 ms apart) — matches verified working pattern
            await self._send_raw(frame)
            await asyncio.sleep(0.2)
            await self._send_raw(frame)
            _LOGGER.debug("send_command %s: sent x2", key)

            # Wait for confirmation
            if await self._wait_for(check_fn, verify_timeout):
                _LOGGER.info("send_command %s: confirmed", key)
                return True

            # Not confirmed in time — re-read (catches delayed EW11 execution)
            await asyncio.sleep(retry_pause)
            if check_fn():
                _LOGGER.info("send_command %s: confirmed (delayed execution)", key)
                return True

            # Retry once
            _LOGGER.warning("send_command %s: no confirmation, retrying", key)
            await self._send_raw(frame)
            await asyncio.sleep(0.2)
            await self._send_raw(frame)

            if await self._wait_for(check_fn, verify_timeout):
                _LOGGER.info("send_command %s: confirmed (retry)", key)
                return True

            await asyncio.sleep(retry_pause)
            result = check_fn()
            if result:
                _LOGGER.info("send_command %s: confirmed (retry delayed)", key)
            else:
                _LOGGER.error("send_command %s: FAILED after retry", key)
            return result

    async def _send_raw(self, data: bytes) -> None:
        if self._writer is None or self._writer.is_closing():
            _LOGGER.warning("_send_raw: no active connection")
            return
        try:
            self._writer.write(data)
            await self._writer.drain()
        except Exception as exc:
            _LOGGER.error("_send_raw error: %s", exc)

    async def _wait_for(self, fn, timeout: float) -> bool:
        """Poll fn() every 100 ms until it returns True or timeout elapses."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if fn():
                return True
            await asyncio.sleep(0.1)
        return False

    # ── internals ─────────────────────────────────────────────────────────────

    async def _connect_and_read(self) -> None:
        _LOGGER.info("Connecting to %s:%d …", self.host, self.port)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=CONNECT_TIMEOUT,
        )
        _LOGGER.info("Connected to %s:%d", self.host, self.port)
        self._writer = writer
        try:
            while self._running:
                try:
                    chunk = await asyncio.wait_for(
                        reader.read(512), timeout=READ_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    # Bus heartbeats at 10 Hz — silence for 5 s means the link is gone
                    raise ConnectionResetError("Read timeout — link lost")

                if not chunk:
                    raise ConnectionResetError("Server closed connection")

                if self.parser.feed(chunk):
                    self._fire_callbacks()
        finally:
            self._writer = None
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            _LOGGER.info("Disconnected from %s:%d", self.host, self.port)

    def _fire_callbacks(self) -> None:
        for cb in self._callbacks:
            try:
                cb(self.parser.state)
            except Exception:
                _LOGGER.exception("Callback raised an exception")


# ── standalone demo ───────────────────────────────────────────────────────────

async def _demo(host: str, port: int, duration: int) -> None:
    ctrl = ProLogicController(host, port)

    def on_change(state: dict) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        print(
            f"\n[{ts}] state change  "
            f"(frames={ctrl.parser.frame_count}  bad_cs={ctrl.parser.bad_checksum})"
        )
        print(format_state(state))

    ctrl.register_callback(on_change)
    task = asyncio.create_task(ctrl.run())

    try:
        if duration:
            await asyncio.sleep(duration)
        else:
            await asyncio.Future()   # run until Ctrl+C
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await ctrl.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    print(f"\nDone. {ctrl.parser.frame_count} frames, {ctrl.parser.bad_checksum} bad checksum.")
    print("\nFinal state:")
    print(format_state(ctrl.state))


if __name__ == "__main__":
    import argparse
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="ProLogic controller — READ-ONLY async demo")
    ap.add_argument("--host",     default=HOST_DEFAULT)
    ap.add_argument("--port",     type=int, default=PORT_DEFAULT)
    ap.add_argument("--duration", type=int, default=30, metavar="S",
                    help="Seconds to run (0 = forever, default 30)")
    args = ap.parse_args()
    try:
        asyncio.run(_demo(args.host, args.port, args.duration))
    except KeyboardInterrupt:
        pass

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

Phase 2: read-only.  send_key() will be added in Phase 4.
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

    # ── internals ─────────────────────────────────────────────────────────────

    async def _connect_and_read(self) -> None:
        _LOGGER.info("Connecting to %s:%d …", self.host, self.port)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=CONNECT_TIMEOUT,
        )
        _LOGGER.info("Connected to %s:%d", self.host, self.port)
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

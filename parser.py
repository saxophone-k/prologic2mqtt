#!/usr/bin/env python3
"""
ProLogic PS4 — stateful RS-485 bus parser.

Import:
    from parser import ProLogicParser
    p = ProLogicParser()
    changed = p.feed(raw_bytes)
    print(p.state)

Run as a live demo:
    python parser.py [--host IP] [--port PORT] [--duration S]

Run unit tests:
    python parser.py --test
"""

import logging
import re
import socket
import sys
import time
from datetime import datetime

_log = logging.getLogger(__name__)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOST_DEFAULT = "192.168.107.61"
PORT_DEFAULT  = 8899

# ── Frame-level plumbing ───────────────────────────────────────────────────────

def extract_frames(buf: bytes) -> tuple[list[bytes], bytes]:
    """Return (payloads, leftover). Un-stuffs 10 10 -> 0x10."""
    frames, i, n = [], 0, len(buf)
    while True:
        start = buf.find(b"\x10\x02", i)
        if start == -1:
            tail = buf[-1:] if (buf and buf[-1] == 0x10) else b""
            return frames, tail
        j = start + 2
        payload, ended = bytearray(), False
        while j < n:
            if buf[j] == 0x10:
                if j + 1 >= n:
                    return frames, buf[start:]
                nb = buf[j + 1]
                if nb == 0x03:
                    ended = True; j += 2; break
                elif nb == 0x02:
                    break
                else:
                    payload.append(0x10); j += 2
            else:
                payload.append(buf[j]); j += 1
        if ended:
            frames.append(bytes(payload)); i = j
        elif j >= n:
            return frames, buf[start:]
        else:
            i = start + 2


def checksum_ok(payload: bytes) -> bool:
    if len(payload) < 4:
        return False
    body = b"\x10\x02" + payload[:-2]
    return (sum(body) & 0xFFFF) == int.from_bytes(payload[-2:], "big")


# ── LED bitmask ────────────────────────────────────────────────────────────────

_HEATER_BIT      = (0, 0x01)   # heat pump relay energised (Auto Control + below setpoint)
_POOL_BIT        = (0, 0x08)
_SPA_BIT         = (0, 0x10)
_FILTER_BIT      = (0, 0x20)
_AUX1_BIT        = (0, 0x80)   # spa jets booster pump
_SUPER_CHLOR_BIT = (3, 0x02)


def _led_bit(on_bytes: bytes, byte_idx: int, mask: int) -> bool:
    return bool(on_bytes[byte_idx] & mask) if len(on_bytes) > byte_idx else False


# ── Text-screen regex patterns ─────────────────────────────────────────────────

_RE_POOL_TEMP    = re.compile(r"Pool\s+Temp\s+(\d+).F",                       re.I)
_RE_SPA_TEMP     = re.compile(r"Spa\s+Temp\s+(\d+).F",                        re.I)
_RE_AIR_TEMP     = re.compile(r"Air\s+Temp\s+(\d+).F",                        re.I)
_RE_SALT         = re.compile(r"Salt\s+Level\s+(\d+)\s+PPM",                  re.I)
_RE_POOL_SWG     = re.compile(r"Pool\s+Chlorinator\s+(\d+)%",                 re.I)
_RE_SPA_SWG      = re.compile(r"Spa\s+Chlorinator\s+(\d+)%",                  re.I)
# Jets timer before spa timer — checked first so "Spa Jets" can't match the spa pattern
_RE_JETS_TIMER   = re.compile(r"Jets.{0,10}CountDn\s+(\d+:\d+)\s+remaining",  re.I)
_RE_SPA_TIMER    = re.compile(r"Spa.{0,5}CountDn\s+(\d+:\d+)\s+remaining",    re.I)
_RE_CHLOR_TIMER  = re.compile(r"Super\s+Chlorinate\s+(\d+:\d+)\s+remaining",  re.I)

_RE_HEAT_MODE = re.compile(r"Heat\s+Pump\s+(Auto\s+Control|Manual\s+Off)", re.I)

_TEXT_INT_FIELDS = [
    (_RE_POOL_TEMP,  "pool_temp_f"),
    (_RE_SPA_TEMP,   "spa_temp_f"),
    (_RE_AIR_TEMP,   "air_temp_f"),
    (_RE_SALT,       "salt_ppm"),
    (_RE_POOL_SWG,   "pool_swg_pct"),
    (_RE_SPA_SWG,    "spa_swg_pct"),
]
_TEXT_STR_FIELDS = [
    (_RE_JETS_TIMER,  "jets_timer_remaining"),
    (_RE_SPA_TIMER,   "spa_timer_remaining"),
    (_RE_CHLOR_TIMER, "super_chlor_remaining"),
]


# ── Parser ────────────────────────────────────────────────────────────────────

class ProLogicParser:
    """
    Stateful RS-485 parser for Hayward ProLogic PS4.

    state dict keys
    ---------------
    mode                  "POOL" | "SPA" | "TRANSITION" | None
    filter_running        bool | None
    jets_on               bool | None   (AUX 1 relay)
    super_chlor_on        bool | None
    heater_on             bool | None   (heat pump relay energised — True only when running,
                                         not merely in Auto Control mode)
    heat_pump_mode        "auto" | "off" | None  (from display text; independent of heater_on)
    air_temp_f            int | None    (binary primary; text cross-checked)
    pool_temp_f           int | None    (short-8c primary; text cross-checked)
    pool_setpoint_f       int | None    (long-8c body[4]+32; very likely pool setpoint, unconfirmed by setpoint-change test)
    spa_temp_f            int | None    (text only)
    salt_ppm              int | None    (long-8c primary; text cross-checked)
    pool_swg_pct          int | None    (text only)
    spa_swg_pct           int | None    (text only)
    spa_timer_remaining   "H:MM" | None (text only)
    jets_timer_remaining  "H:MM" | None (text only)
    super_chlor_remaining "H:MM" | None (text only)
    panel_clock           {"hour": int, "minute": int, "dow": int} | None
    led_on_bytes          bytes | None
    validation_warnings   list[str]
    """

    def __init__(self):
        self._buf: bytes = b""
        self.state: dict = {
            "mode":                   None,
            "filter_running":         None,
            "jets_on":                None,
            "super_chlor_on":         None,
            "heater_on":              None,
            "heat_pump_mode":         None,
            "air_temp_f":             None,
            "pool_temp_f":            None,
            "pool_setpoint_f":        None,
            "spa_temp_f":             None,
            "salt_ppm":               None,
            "pool_swg_pct":           None,
            "spa_swg_pct":            None,
            "spa_timer_remaining":    None,
            "jets_timer_remaining":   None,
            "super_chlor_remaining":  None,
            "panel_clock":            None,
            "led_on_bytes":           None,
            "validation_warnings":    [],
        }
        # Shadow values for cross-validation (set only from binary sources)
        self._bin_air:  int | None = None
        self._bin_pool: int | None = None
        self._bin_salt: int | None = None
        self.frame_count  = 0
        self.bad_checksum = 0

    def feed(self, data: bytes) -> bool:
        """Feed raw TCP bytes. Returns True if any state field changed."""
        self._buf += data
        frames, self._buf = extract_frames(self._buf)
        changed = False
        for payload in frames:
            self.frame_count += 1
            if not checksum_ok(payload):
                self.bad_checksum += 1
                continue
            changed |= self._apply(payload)
        return changed

    # ── dispatch ──────────────────────────────────────────────────────────────

    def _apply(self, payload: bytes) -> bool:
        if len(payload) < 4:
            return False
        tk   = payload[:2]
        data = payload[2:-2]   # strip type bytes and checksum
        if tk == b"\x01\x02":
            return self._apply_led(data)
        if tk == b"\x01\x03":
            return self._apply_display(data)
        if tk == b"\x04\x0a":
            return self._apply_long_display(data)
        return False

    # ── LED_STATE (01 02) ─────────────────────────────────────────────────────

    def _apply_led(self, data: bytes) -> bool:
        if len(data) < 8:
            return False
        on = data[:4]
        s  = self.state

        heater    = _led_bit(on, *_HEATER_BIT)
        pool_on   = _led_bit(on, *_POOL_BIT)
        spa_on    = _led_bit(on, *_SPA_BIT)
        filter_on = _led_bit(on, *_FILTER_BIT)
        jets      = _led_bit(on, *_AUX1_BIT)
        chlor     = _led_bit(on, *_SUPER_CHLOR_BIT)

        # Mode: either bit set without filter = 35 s valve transition
        if (pool_on or spa_on) and not filter_on:
            mode = "TRANSITION"
        elif pool_on:
            mode = "POOL"
        elif spa_on:
            mode = "SPA"
        else:
            mode = None

        changed = False
        for key, val in [
            ("mode",           mode),
            ("filter_running", filter_on),
            ("jets_on",        jets),
            ("super_chlor_on", chlor),
            ("heater_on",      heater),
            ("led_on_bytes",   on),
        ]:
            if s[key] != val:
                s[key] = val
                changed = True

        return changed

    # ── DISPLAY (01 03) ───────────────────────────────────────────────────────

    def _apply_display(self, data: bytes) -> bool:
        if len(data) < 40:
            return False
        text = data[:40].decode("latin-1")
        return self._apply_text(text)

    # ── LONG_DISPLAY (04 0a) ──────────────────────────────────────────────────

    def _apply_long_display(self, data: bytes) -> bool:
        if not data:
            return False
        sub = data[0]
        if sub == 0x83:
            return self._apply_long_83(data)
        if sub == 0x8c:
            return self._apply_long_8c(data)
        return False

    def _apply_long_83(self, data: bytes) -> bool:
        if len(data) < 3:
            return False
        variant = data[2]
        if variant == 0x02 and len(data) >= 52:
            return self._apply_text(data[12:52].decode("latin-1"))
        if variant == 0x03 and len(data) >= 43:
            return self._apply_text(data[3:43].decode("latin-1"))
        return False

    def _apply_long_8c(self, data: bytes) -> bool:
        """
        Short body (10 bytes): sent every cycle ~1 Hz.
          body[2]=minute, body[3]=hour, body[4]=dow, body[5]=air+40, body[6]=water+40
        Long body (23 bytes): sent ~every 19 s when Aqua Pod base polls.
          body[4]=pool_setpoint+32 (NOT actual temp — fixed until setpoint changes), body[20]=salt/100
        The two variants use different encodings and carry different information.
        """
        body = data[1:]   # skip 0x8c marker
        changed = False
        s = self.state

        if len(body) >= 23:
            # Long 8c — body[4] is the pool SETPOINT (fixed until user changes it),
            # not the actual water temp; the short 8c body[6] carries the live reading.
            setpoint_f = body[4] + 32
            salt       = body[20] * 100
            if 0 <= setpoint_f <= 150:
                if s["pool_setpoint_f"] != setpoint_f:
                    s["pool_setpoint_f"] = setpoint_f; changed = True
            if 0 <= salt <= 9900:
                self._bin_salt = salt
                if s["salt_ppm"] != salt:
                    s["salt_ppm"] = salt; changed = True

        elif len(body) >= 10:
            # Short 8c
            air_f  = body[5] - 40
            pool_f = body[6] - 40
            clock  = {"hour": body[3], "minute": body[2], "dow": body[4]}

            if s["panel_clock"] != clock:
                s["panel_clock"] = clock; changed = True
            if 0 <= air_f <= 150:
                self._bin_air = air_f
                if s["air_temp_f"] != air_f:
                    s["air_temp_f"] = air_f; changed = True
            if 0 <= pool_f <= 150:
                self._bin_pool = pool_f
                if s["pool_temp_f"] != pool_f:
                    s["pool_temp_f"] = pool_f; changed = True

        if changed:
            self._cross_check()
        return changed

    # ── text screen parser ────────────────────────────────────────────────────

    def _apply_text(self, text: str) -> bool:
        """Match any recognized patterns in a 40-char screen; update state."""
        text = text.replace('\xba', ':')   # panel uses 0xBA as colon separator
        _log.debug("Display: %r", text.strip())
        s = self.state
        changed = False

        for pattern, key in _TEXT_INT_FIELDS:
            m = pattern.search(text)
            if m:
                v = int(m.group(1))
                if s.get(key) != v:
                    s[key] = v; changed = True

        for pattern, key in _TEXT_STR_FIELDS:
            m = pattern.search(text)
            if m:
                v = m.group(1)
                if s.get(key) != v:
                    s[key] = v; changed = True

        m = _RE_HEAT_MODE.search(text)
        if m:
            v = "auto" if "auto" in m.group(1).lower() else "off"
            if s.get("heat_pump_mode") != v:
                s["heat_pump_mode"] = v; changed = True

        if changed:
            self._cross_check()
        return changed

    # ── cross-validation ──────────────────────────────────────────────────────

    def _cross_check(self):
        s = self.state
        w = s["validation_warnings"]

        def chk(field, binary_val, tol):
            txt_val = s.get(field)
            if binary_val is None or txt_val is None:
                return
            if abs(binary_val - txt_val) > tol:
                msg = f"mismatch {field}: binary={binary_val} text={txt_val}"
                if not w or w[-1] != msg:
                    w.append(msg)

        chk("air_temp_f",  self._bin_air,  tol=2)
        chk("pool_temp_f", self._bin_pool, tol=2)
        chk("salt_ppm",    self._bin_salt, tol=200)


# ── Pretty-printer ────────────────────────────────────────────────────────────

_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

def format_state(state: dict) -> str:
    s = state
    on  = s.get("led_on_bytes")
    clk = s.get("panel_clock")
    clk_str = (f"{clk['hour']:02d}:{clk['minute']:02d} "
               f"{_DOW[clk['dow'] - 1]}" if clk else "None")
    lines = [
        f"  mode              : {s.get('mode')}",
        f"  filter_running    : {s.get('filter_running')}",
        f"  jets_on           : {s.get('jets_on')}",
        f"  super_chlor_on    : {s.get('super_chlor_on')}",
        f"  heater_on         : {s.get('heater_on')}",
        f"  heat_pump_mode    : {s.get('heat_pump_mode')}",
        f"  air_temp_f        : {s.get('air_temp_f')}",
        f"  pool_temp_f       : {s.get('pool_temp_f')}",
        f"  pool_setpoint_f   : {s.get('pool_setpoint_f')}",
        f"  spa_temp_f        : {s.get('spa_temp_f')}",
        f"  salt_ppm          : {s.get('salt_ppm')}",
        f"  pool_swg_pct      : {s.get('pool_swg_pct')}",
        f"  spa_swg_pct       : {s.get('spa_swg_pct')}",
        f"  spa_timer         : {s.get('spa_timer_remaining')}",
        f"  jets_timer        : {s.get('jets_timer_remaining')}",
        f"  super_chlor_rem   : {s.get('super_chlor_remaining')}",
        f"  panel_clock       : {clk_str}",
        f"  led_on_bytes      : {on.hex(' ') if on else 'None'}",
    ]
    if s.get("validation_warnings"):
        lines.append("  WARNINGS:")
        for msg in s["validation_warnings"][-5:]:
            lines.append(f"    ! {msg}")
    return "\n".join(lines)


# ── Unit tests ────────────────────────────────────────────────────────────────

def _build_frame(type_bytes: bytes, data: bytes) -> bytes:
    """Construct a valid framed packet for testing."""
    inner = type_bytes + data
    cs = (0x10 + 0x02 + sum(inner)) & 0xFFFF
    payload = inner + cs.to_bytes(2, "big")
    stuffed = bytearray()
    for b in payload:
        stuffed.append(b)
        if b == 0x10:
            stuffed.append(0x10)
    return b"\x10\x02" + bytes(stuffed) + b"\x10\x03"


def _run_tests() -> bool:
    passed = failed = 0

    def check(label: str, actual, expected):
        nonlocal passed, failed
        if actual == expected:
            print(f"  PASS  {label}")
            passed += 1
        else:
            print(f"  FAIL  {label}: got {actual!r}, expected {expected!r}")
            failed += 1

    def screen(text: str) -> bytes:
        """Build a 41-byte DISPLAY data field from a string."""
        return text.ljust(40).encode("ascii", errors="replace") + b"\x00"

    # --- LED_STATE ---
    p = ProLogicParser()

    # KEEPALIVE changes nothing
    check("KEEPALIVE no change", p.feed(_build_frame(b"\x01\x01", b"")), False)

    # Pool idle: byte0 = POOL(0x08) | FILTER(0x20) = 0x28
    p.feed(_build_frame(b"\x01\x02", bytes([0x28,0,0,0, 0,0,0,0])))
    check("LED pool mode",      p.state["mode"],           "POOL")
    check("LED filter on",      p.state["filter_running"],  True)
    check("LED jets off",       p.state["jets_on"],         False)
    check("LED super_chlor off",p.state["super_chlor_on"],  False)
    check("LED on_bytes",       p.state["led_on_bytes"],    bytes([0x28,0,0,0]))

    # Spa mode: byte0 = SPA(0x10) | FILTER(0x20) = 0x30
    p.feed(_build_frame(b"\x01\x02", bytes([0x30,0,0,0, 0,0,0,0])))
    check("LED spa mode",       p.state["mode"],            "SPA")

    # Jets ON: add AUX1(0x80)
    p.feed(_build_frame(b"\x01\x02", bytes([0xB0,0,0,0, 0,0,0,0])))
    check("LED jets on",        p.state["jets_on"],         True)

    # Super chlorinate ON: byte3 = 0x02
    p.feed(_build_frame(b"\x01\x02", bytes([0x28,0,0,2, 0,0,0,0])))
    check("LED super_chlor on", p.state["super_chlor_on"],  True)

    # Transition Spa->Pool: POOL bit set, FILTER off = 0x08
    p.feed(_build_frame(b"\x01\x02", bytes([0x08,0,0,0, 0,0,0,0])))
    check("LED transition mode",    p.state["mode"],           "TRANSITION")
    check("LED transition filter",  p.state["filter_running"],  False)

    # --- DISPLAY text parsing ---
    p2 = ProLogicParser()

    p2.feed(_build_frame(b"\x01\x03", screen("  Pool Temp  84\xbaF")))
    check("DISPLAY pool_temp_f",  p2.state["pool_temp_f"], 84)

    p2.feed(_build_frame(b"\x01\x03", screen("  Air Temp   75\xbaF")))
    check("DISPLAY air_temp_f",   p2.state["air_temp_f"],  75)

    p2.feed(_build_frame(b"\x01\x03", screen("  Spa Temp   92\xbaF")))
    check("DISPLAY spa_temp_f",   p2.state["spa_temp_f"],  92)

    p2.feed(_build_frame(b"\x01\x03", screen("     Salt Level        3100 PPM")))
    check("DISPLAY salt_ppm",     p2.state["salt_ppm"],    3100)

    p2.feed(_build_frame(b"\x01\x03", screen("  Pool Chlorinator        20%")))
    check("DISPLAY pool_swg_pct", p2.state["pool_swg_pct"], 20)

    p2.feed(_build_frame(b"\x01\x03", screen("   Spa-CountDn   3:57 remaining")))
    check("DISPLAY spa_timer",    p2.state["spa_timer_remaining"],  "3:57")

    p2.feed(_build_frame(b"\x01\x03", screen("  Spa Jets -CountDn   3:59 remaining")))
    check("DISPLAY jets_timer",   p2.state["jets_timer_remaining"], "3:59")

    p2.feed(_build_frame(b"\x01\x03", screen("  Super Chlorinate   23:58 remaining")))
    check("DISPLAY super_chlor_rem", p2.state["super_chlor_remaining"], "23:58")

    # Jets timer doesn't accidentally match spa timer pattern
    p3 = ProLogicParser()
    p3.feed(_build_frame(b"\x01\x03", screen("  Spa Jets -CountDn   2:00 remaining")))
    check("Jets timer doesn't set spa_timer", p3.state["spa_timer_remaining"], None)
    check("Jets timer sets jets_timer",       p3.state["jets_timer_remaining"], "2:00")

    # --- Short 8c binary ---
    # air=75 F -> 75+40=115=0x73, pool=84 F -> 84+40=124=0x7c
    # hour=21=0x15, minute=37=0x25, dow=7 (Sunday)
    short_body = bytes([0x00, 0x0f, 0x25, 0x15, 0x07, 0x73, 0x7c, 0x00, 0x42, 0x25])
    p4 = ProLogicParser()
    p4.feed(_build_frame(b"\x04\x0a", bytes([0x8c]) + short_body))
    check("8c-short air_temp_f",    p4.state["air_temp_f"],  75)
    check("8c-short pool_temp_f",   p4.state["pool_temp_f"], 84)
    check("8c-short clock hour",    p4.state["panel_clock"]["hour"],   21)
    check("8c-short clock minute",  p4.state["panel_clock"]["minute"], 37)
    check("8c-short clock dow",     p4.state["panel_clock"]["dow"],    7)

    # --- Long 8c binary ---
    # pool=84 F -> 84-32=52, salt=3100 PPM -> 31
    long_body = bytearray(23)
    long_body[4]  = 52   # pool temp: byte + 32
    long_body[20] = 31   # salt: byte * 100
    p5 = ProLogicParser()
    p5.feed(_build_frame(b"\x04\x0a", bytes([0x8c]) + bytes(long_body)))
    check("8c-long pool_setpoint_f", p5.state["pool_setpoint_f"], 84)
    check("8c-long pool_temp_f none",p5.state["pool_temp_f"],     None)
    check("8c-long salt_ppm",        p5.state["salt_ppm"],        3100)
    # Clock must NOT be written by long-8c path
    check("8c-long no clock",     p5.state["panel_clock"], None)

    # --- Cross-check validation ---
    p6 = ProLogicParser()
    # Binary says air=75 F
    p6.feed(_build_frame(b"\x04\x0a", bytes([0x8c]) + short_body))
    # Text says air=80 F (>2 F mismatch)
    p6.feed(_build_frame(b"\x01\x03", screen("  Air Temp   80\xbaF")))
    has_warn = any("air_temp_f" in w for w in p6.state["validation_warnings"])
    check("Cross-check warning issued",  has_warn, True)

    # --- HEATER LED bit ---
    # Spa + filter + heater active: byte0 = SPA(0x10)|FILTER(0x20)|HEATER(0x01) = 0x31
    p8 = ProLogicParser()
    p8.feed(_build_frame(b"\x01\x02", bytes([0x31,0,0,0, 0,0,0,0])))
    check("LED heater_on True",      p8.state["heater_on"],      True)
    check("LED spa + heater mode",   p8.state["mode"],           "SPA")
    check("LED filter on (heater)",  p8.state["filter_running"],  True)

    # Heater off (pool idle): byte0 = 0x28
    p8.feed(_build_frame(b"\x01\x02", bytes([0x28,0,0,0, 0,0,0,0])))
    check("LED heater_on False",     p8.state["heater_on"],      False)

    # --- heat_pump_mode from display text ---
    p9 = ProLogicParser()
    p9.feed(_build_frame(b"\x01\x03", screen("     Heat Pump      Auto Control")))
    check("DISPLAY heat_pump_mode auto", p9.state["heat_pump_mode"], "auto")

    p9.feed(_build_frame(b"\x01\x03", screen("     Heat Pump      Manual Off")))
    check("DISPLAY heat_pump_mode off",  p9.state["heat_pump_mode"], "off")

    # --- Checksum rejection ---
    # Build a valid frame then corrupt a data byte
    raw = bytearray(_build_frame(b"\x01\x02", bytes([0x28,0,0,0, 0,0,0,0])))
    raw[4] ^= 0xFF   # flip a byte inside the payload
    p7 = ProLogicParser()
    p7.feed(bytes(raw))
    check("Bad checksum rejected",    p7.bad_checksum, 1)
    check("Bad checksum no state",    p7.state["mode"], None)

    total = passed + failed
    print(f"\n{passed}/{total} passed")
    return failed == 0


# ── Live demo ──────────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="ProLogic parser — READ-ONLY live demo")
    ap.add_argument("--host",     default=HOST_DEFAULT)
    ap.add_argument("--port",     type=int, default=PORT_DEFAULT)
    ap.add_argument("--duration", type=int, default=0, metavar="S",
                    help="Stop after N seconds (0 = Ctrl+C)")
    ap.add_argument("--test",     action="store_true",
                    help="Run unit tests and exit")
    args = ap.parse_args()

    if args.test:
        print("Running unit tests...\n")
        ok = _run_tests()
        sys.exit(0 if ok else 1)

    print("ProLogic parser — READ-ONLY live demo")
    print(f"Connecting to {args.host}:{args.port} ...")
    try:
        sock = socket.create_connection((args.host, args.port), timeout=10)
        sock.settimeout(1.0)
    except OSError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    print("Connected. Waiting for data...\n")

    parser  = ProLogicParser()
    t0      = time.time()
    last_ts = 0.0

    try:
        while True:
            if args.duration and (time.time() - t0) >= args.duration:
                break
            try:
                chunk = sock.recv(512)
            except socket.timeout:
                continue
            if not chunk:
                break
            changed = parser.feed(chunk)
            now = time.time()
            if changed and (now - last_ts) >= 1.0:
                last_ts = now
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] frames={parser.frame_count}  bad_cs={parser.bad_checksum}")
                print(format_state(parser.state))
                print()
    except KeyboardInterrupt:
        pass

    print(f"\nDone. {parser.frame_count} frames, {parser.bad_checksum} bad checksum.")
    print("\nFinal state:")
    print(format_state(parser.state))


if __name__ == "__main__":
    main()

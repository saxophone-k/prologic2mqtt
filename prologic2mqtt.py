#!/usr/bin/env python3
"""
prologic2mqtt — Hayward ProLogic PS4 -> MQTT -> Home Assistant

Reads the RS-485 bus via an Elfin EW11 WiFi bridge and publishes all pool
state to MQTT. MQTT Discovery auto-creates every entity in Home Assistant.

Environment variables (P2M_* prefix):
    P2M_EW11_HOST           EW11 IP address         (default: 192.168.107.61)
    P2M_EW11_PORT           EW11 TCP port            (default: 8899)
    P2M_MQTT_HOST           MQTT broker host         (required)
    P2M_MQTT_PORT           MQTT broker port         (default: 1883)
    P2M_MQTT_USERNAME       MQTT username            (optional)
    P2M_MQTT_PASSWORD       MQTT password            (optional)
    P2M_MQTT_TOPIC_PREFIX   State topic prefix       (default: prologic2mqtt)
    P2M_LOG_LEVEL           Logging level            (default: info)
"""

import asyncio
import json
import logging
import os
import signal

import paho.mqtt.client as mqtt

from controller import (
    ProLogicController,
    CMD_FILTER, CMD_JETS, CMD_POOL_SPA, CMD_HP, CMD_SUPER_CHLOR,
)

# ── Configuration ─────────────────────────────────────────────────────────────

def _env(key, default=None, required=False):
    val = os.environ.get(key, default)
    if required and val is None:
        raise SystemExit(f"Required environment variable missing: {key}")
    return val


CFG = {
    "ew11_host": _env("P2M_EW11_HOST",         "192.168.107.61"),
    "ew11_port": int(_env("P2M_EW11_PORT",       "8899")),
    "mqtt_host": _env("P2M_MQTT_HOST",           required=True),
    "mqtt_port": int(_env("P2M_MQTT_PORT",       "1883")),
    "mqtt_user": _env("P2M_MQTT_USERNAME",       ""),
    "mqtt_pass": _env("P2M_MQTT_PASSWORD",       ""),
    "prefix":    _env("P2M_MQTT_TOPIC_PREFIX",   "prologic2mqtt"),
    "log_level": _env("P2M_LOG_LEVEL",           "info").upper(),
}

logging.basicConfig(
    level=getattr(logging, CFG["log_level"], logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("prologic2mqtt")

_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _fmt_timer(val: str | None) -> str:
    """Convert "H:MM" → "HhMMm", or return "" if inactive."""
    if not val:
        return ""
    h, _, m = val.partition(":")
    return f"{h}h{m}m" if _ else val


# ── Bridge ────────────────────────────────────────────────────────────────────

class ProLogicMQTTBridge:

    def __init__(self):
        self._ctrl     = ProLogicController(CFG["ew11_host"], CFG["ew11_port"])
        self._mqtt     = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION1,
            client_id="prologic2mqtt",
            clean_session=True,
        )
        self._running  = True
        self._last_state: dict = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._transition_task: asyncio.Task | None = None

    # ── Topic helpers ─────────────────────────────────────────────────────────

    def _t(self, suffix: str) -> str:
        return f"{CFG['prefix']}/{suffix}"

    def _disc(self, component: str, key: str) -> str:
        return f"homeassistant/{component}/prologic_{key}/config"

    # ── MQTT Discovery ────────────────────────────────────────────────────────

    def _publish_discovery(self) -> None:
        dev = {
            "identifiers": ["prologic_ew11"],
            "name":         "Hayward ProLogic",
            "manufacturer": "Hayward",
            "model":        "ProLogic PS4",
        }
        avail = {
            "topic":                 self._t("availability"),
            "payload_available":     "online",
            "payload_not_available": "offline",
        }

        def sensor(key, name, *, unit=None, device_class=None,
                   state_class=None, icon=None):
            p = {
                "name":        name,
                "unique_id":   f"prologic_{key}",
                "device":      dev,
                "state_topic": self._t(key),
                "availability": [avail],
            }
            if unit:         p["unit_of_measurement"] = unit
            if device_class: p["device_class"]        = device_class
            if state_class:  p["state_class"]         = state_class
            if icon:         p["icon"]                = icon
            self._mqtt.publish(self._disc("sensor", key), json.dumps(p), retain=True)

        def binary_sensor(key, name, *, device_class=None, icon=None):
            p = {
                "name":        name,
                "unique_id":   f"prologic_{key}",
                "device":      dev,
                "state_topic": self._t(key),
                "payload_on":  "ON",
                "payload_off": "OFF",
                "availability": [avail],
            }
            if device_class: p["device_class"] = device_class
            if icon:         p["icon"]         = icon
            self._mqtt.publish(self._disc("binary_sensor", key),
                               json.dumps(p), retain=True)

        def switch(key, name, *, icon=None):
            p = {
                "name":            name,
                "unique_id":       f"prologic_{key}",
                "device":          dev,
                "state_topic":     self._t(key),
                "command_topic":   self._t(f"{key}/set"),
                "payload_on":      "ON",
                "payload_off":     "OFF",
                "optimistic":      False,
                "availability":    [avail],
            }
            if icon: p["icon"] = icon
            self._mqtt.publish(self._disc("switch", key), json.dumps(p), retain=True)

        def remove(component, key):
            """Tell HA to delete a previously discovered entity."""
            self._mqtt.publish(self._disc(component, key), "", retain=True)

        # ── Remove stale entities from previous versions ──────────────────────
        remove("sensor",        "pool_temp")      # merged → water_temp
        remove("sensor",        "spa_temp")       # merged → water_temp
        remove("sensor",        "pool_swg")       # merged → chlorinator
        remove("sensor",        "spa_swg")        # merged → chlorinator
        remove("binary_sensor", "heater")         # replaced → heat_pump_activity sensor
        remove("binary_sensor", "filter_pump")    # promoted → switch
        remove("binary_sensor", "jets")           # promoted → switch
        remove("binary_sensor", "super_chlor")    # promoted → switch
        remove("sensor",        "pool_setpoint")  # removed — not reliably available on bus
        remove("sensor",        "spa_setpoint")   # removed — not reliably available on bus

        # ── Sensors ───────────────────────────────────────────────────────────
        sensor("water_temp",  "Water Temperature",
               unit="°F", device_class="temperature", state_class="measurement")
        sensor("air_temp",    "Air Temperature",
               unit="°F", device_class="temperature", state_class="measurement")
        sensor("chlorinator", "Chlorinator Level",
               unit="%", state_class="measurement", icon="mdi:water-percent")
        sensor("salt_ppm",    "Salt Level",
               unit="ppm", state_class="measurement", icon="mdi:shaker-outline")
        sensor("mode",        "Mode",             icon="mdi:pool")
        sensor("heat_pump_mode",     "Heat Pump Mode",     icon="mdi:heat-pump-outline")
        sensor("heat_pump_activity", "Heat Pump Activity", icon="mdi:heat-pump")
        sensor("spa_timer",   "Spa Timer",        icon="mdi:timer-outline")
        sensor("jets_timer",  "Spa Jets Timer",   icon="mdi:timer-outline")
        sensor("super_chlor_timer", "Super Chlorinate Timer", icon="mdi:timer-outline")
        sensor("panel_clock", "Panel Clock",      icon="mdi:clock-outline")
        sensor("transition_remaining", "Valve Transition",
               unit="s", icon="mdi:valve")

        # ── Switches ──────────────────────────────────────────────────────────
        switch("filter_pump", "Filter Pump",      icon="mdi:pump")
        switch("jets",        "Spa Jets",         icon="mdi:turbine")
        switch("super_chlor", "Super Chlorinate", icon="mdi:flask-outline")
        switch("pool_spa",    "Spa Mode",         icon="mdi:pool")
        switch("heat_pump",   "Heat Pump",        icon="mdi:heat-pump-outline")

        log.info("MQTT Discovery published — 12 sensors + 5 switches")

    # ── State publishing ──────────────────────────────────────────────────────

    def _on_state_change(self, state: dict) -> None:
        """Fires from the asyncio event loop on every parsed state change."""
        t = self._t

        def pub(topic, value):
            if value is not None:
                self._mqtt.publish(t(topic), str(value))

        mode = state.get("mode")

        # Transition countdown — start on entry, cancel on exit
        if mode == "TRANSITION":
            if self._transition_task is None or self._transition_task.done():
                self._transition_task = asyncio.create_task(self._transition_countdown())
        else:
            if self._transition_task is not None and not self._transition_task.done():
                self._transition_task.cancel()
            self._mqtt.publish(self._t("transition_remaining"), "0")

        # Water temperature: spa temp when in spa mode, pool temp otherwise
        if mode == "SPA":
            water_temp = state.get("spa_temp_f") or state.get("pool_temp_f")
        else:
            water_temp = state.get("pool_temp_f")
        pub("water_temp", water_temp)

        pub("air_temp",       state.get("air_temp_f"))
        pub("salt_ppm",       state.get("salt_ppm"))
        pub("mode",           mode)
        pub("panel_clock",    None)   # handled separately below

        # Chlorinator level: spa value in spa mode, pool value otherwise
        if mode == "SPA":
            chlor = state.get("spa_swg_pct")
        else:
            chlor = state.get("pool_swg_pct")
        pub("chlorinator", chlor)

        # Heat Pump Mode: display panel labels instead of internal values
        hp_mode = state.get("heat_pump_mode")
        if hp_mode == "auto":
            self._mqtt.publish(t("heat_pump_mode"), "Auto Control")
        elif hp_mode == "off":
            self._mqtt.publish(t("heat_pump_mode"), "Manual Off")

        # Heat Pump Activity: "Heating" when relay energised, "Off" otherwise
        heater_on = state.get("heater_on")
        if heater_on is not None:
            self._mqtt.publish(t("heat_pump_activity"),
                               "Heating" if heater_on else "Off")

        # Timer fields: clear immediately when the corresponding feature is off
        self._mqtt.publish(t("spa_timer"),
            _fmt_timer(state.get("spa_timer_remaining")) if mode == "SPA" else "")
        if state.get("jets_on"):
            jets_timer = (_fmt_timer(state.get("spa_timer_remaining")) if mode == "SPA"
                          else _fmt_timer(state.get("jets_timer_remaining")))
        else:
            jets_timer = ""
        self._mqtt.publish(t("jets_timer"), jets_timer)
        self._mqtt.publish(t("super_chlor_timer"),
            _fmt_timer(state.get("super_chlor_remaining")) if state.get("super_chlor_on") else "")

        # Panel clock
        clk = state.get("panel_clock")
        if clk:
            self._mqtt.publish(
                t("panel_clock"),
                f"{clk['hour']:02d}:{clk['minute']:02d} {_DOW[clk['dow'] - 1]}",
            )

        # Switch states — filter, jets, super_chlor
        for topic, key in [
            ("filter_pump", "filter_running"),
            ("jets",        "jets_on"),
            ("super_chlor", "super_chlor_on"),
        ]:
            val = state.get(key)
            if val is not None:
                self._mqtt.publish(t(topic), "ON" if val else "OFF")

        # pool_spa switch: ON = Spa Mode active, OFF = Pool Mode active
        if mode == "SPA":
            self._mqtt.publish(t("pool_spa"), "ON")
        elif mode == "POOL":
            self._mqtt.publish(t("pool_spa"), "OFF")
        # TRANSITION: don't update — leave switch showing last confirmed mode

        # heat_pump switch: ON = Auto Control, OFF = Manual Off
        hp_mode = state.get("heat_pump_mode")
        if hp_mode == "auto":
            self._mqtt.publish(t("heat_pump"), "ON")
        elif hp_mode == "off":
            self._mqtt.publish(t("heat_pump"), "OFF")

        # Mark online after first valid frame
        if self._ctrl.available:
            self._mqtt.publish(t("availability"), "online", retain=True)

        # Log only fields that actually changed (skip noisy/internal keys)
        _skip = {"panel_clock", "led_on_bytes", "validation_warnings"}
        changed = {k: v for k, v in state.items()
                   if k not in _skip and v != self._last_state.get(k)}
        if changed:
            log.info("State update: %s", changed)
        self._last_state = dict(state)

    async def _transition_countdown(self) -> None:
        """Publish transition_remaining every second from 35 down to 0."""
        try:
            for remaining in range(35, -1, -1):
                self._mqtt.publish(self._t("transition_remaining"), str(remaining))
                if remaining > 0:
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            self._transition_task = None

    # ── MQTT callbacks ────────────────────────────────────────────────────────

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info("MQTT connected to %s:%d", CFG["mqtt_host"], CFG["mqtt_port"])
            self._mqtt.publish(self._t("availability"), "online", retain=True)
            self._mqtt.subscribe(f"{CFG['prefix']}/+/set")
        else:
            log.error("MQTT connection failed: rc=%d", rc)

    def _on_mqtt_message(self, client, userdata, msg):
        """Called from the paho thread when a command arrives on a /set topic."""
        prefix = CFG["prefix"] + "/"
        topic  = msg.topic
        if not (topic.startswith(prefix) and topic.endswith("/set")):
            return
        key     = topic[len(prefix):-4]
        payload = msg.payload.decode("utf-8", errors="ignore").strip().upper()
        if payload not in ("ON", "OFF"):
            log.warning("Ignoring unknown payload %r on %s", payload, topic)
            return
        desired = (payload == "ON")
        log.info("Command received: %s = %s", key, payload)

        # Optimistic immediate state publish for instant UI feedback
        self._mqtt.publish(self._t(key), payload)

        # Schedule the actual command on the asyncio loop
        if self._loop is None:
            log.warning("Event loop not ready, dropping command %s", key)
            return
        asyncio.run_coroutine_threadsafe(
            self._handle_command(key, desired), self._loop
        )

    def _on_mqtt_disconnect(self, client, userdata, rc):
        if rc != 0:
            log.warning("MQTT disconnected (rc=%d) — auto-reconnect in progress", rc)

    # ── Command handler ───────────────────────────────────────────────────────

    async def _handle_command(self, key: str, desired: bool) -> None:
        """
        Async command handler — runs on the asyncio loop.
        Builds the appropriate check functions and calls send_command.
        After the command resolves, the real bus state (from LED_STATE frames)
        will correct any optimistic publish if needed.
        """
        s = self._ctrl.state
        try:
            if key == "filter_pump":
                await self._ctrl.send_command(
                    key, CMD_FILTER,
                    check_fn=lambda: self._ctrl.state.get("filter_running") == desired,
                    pre_check_fn=lambda: self._ctrl.state.get("filter_running") == desired,
                )

            elif key == "jets":
                await self._ctrl.send_command(
                    key, CMD_JETS,
                    check_fn=lambda: self._ctrl.state.get("jets_on") == desired,
                    pre_check_fn=lambda: self._ctrl.state.get("jets_on") == desired,
                )

            elif key == "super_chlor":
                await self._ctrl.send_command(
                    key, CMD_SUPER_CHLOR,
                    check_fn=lambda: self._ctrl.state.get("super_chlor_on") == desired,
                    pre_check_fn=lambda: self._ctrl.state.get("super_chlor_on") == desired,
                    debounce=30.0,
                )

            elif key == "pool_spa":
                target_mode = "SPA" if desired else "POOL"
                await self._ctrl.send_command(
                    key, CMD_POOL_SPA,
                    check_fn=lambda: self._ctrl.state.get("mode") == target_mode,
                    pre_check_fn=lambda: self._ctrl.state.get("mode") == target_mode,
                    verify_timeout=35.0,
                    debounce=30.0,
                )

            elif key == "heat_pump":
                # Capture relay state before sending for the fast-path OFF check.
                # HP toggle is verified via heater relay (fast, LED) or display
                # text (slow, up to 20 s).  Both paths are checked.
                pre_heater = s.get("heater_on")

                if desired:   # → Auto Control
                    def check_fn():
                        cs = self._ctrl.state
                        return bool(cs.get("heater_on")) or cs.get("heat_pump_mode") == "auto"
                    def pre_check_fn():
                        return self._ctrl.state.get("heat_pump_mode") == "auto"
                else:         # → Manual Off
                    def check_fn():
                        cs = self._ctrl.state
                        # Fast path: relay was energised and just opened
                        if pre_heater and not cs.get("heater_on"):
                            return True
                        # Slow path: display text confirmed
                        return cs.get("heat_pump_mode") == "off"
                    def pre_check_fn():
                        return self._ctrl.state.get("heat_pump_mode") == "off"

                await self._ctrl.send_command(
                    key, CMD_HP, check_fn,
                    pre_check_fn=pre_check_fn,
                    verify_timeout=25.0,
                )

            else:
                log.warning("Unknown command key: %s", key)

        except Exception:
            log.exception("Error handling command %s=%s", key, desired)

    # ── Main async loop ───────────────────────────────────────────────────────

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()

        # Set up MQTT
        self._mqtt.on_connect    = self._on_mqtt_connect
        self._mqtt.on_disconnect = self._on_mqtt_disconnect
        self._mqtt.on_message    = self._on_mqtt_message
        if CFG["mqtt_user"]:
            self._mqtt.username_pw_set(CFG["mqtt_user"], CFG["mqtt_pass"])
        self._mqtt.will_set(self._t("availability"), "offline", retain=True)
        self._mqtt.reconnect_delay_set(min_delay=5, max_delay=30)
        self._mqtt.connect(CFG["mqtt_host"], CFG["mqtt_port"], keepalive=60)
        self._mqtt.loop_start()

        await asyncio.sleep(2)      # let MQTT finish connecting
        self._publish_discovery()

        # Start EW11 reader
        self._ctrl.register_callback(self._on_state_change)
        ctrl_task = asyncio.create_task(self._ctrl.run())

        log.info(
            "Bridge running — EW11 %s:%d  ->  MQTT %s:%d",
            CFG["ew11_host"], CFG["ew11_port"],
            CFG["mqtt_host"], CFG["mqtt_port"],
        )

        try:
            while self._running:
                await asyncio.sleep(1)
        finally:
            log.info("Shutting down...")
            await self._ctrl.stop()
            ctrl_task.cancel()
            try:
                await ctrl_task
            except asyncio.CancelledError:
                pass
            self._mqtt.publish(self._t("availability"), "offline", retain=True)
            self._mqtt.loop_stop()
            self._mqtt.disconnect()
            log.info("Bridge stopped")


# ── Entry point ───────────────────────────────────────────────────────────────

async def _main() -> None:
    bridge = ProLogicMQTTBridge()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: setattr(bridge, "_running", False))
        except NotImplementedError:
            pass    # Windows

    await bridge.run()


if __name__ == "__main__":
    log.info("prologic2mqtt starting")
    asyncio.run(_main())

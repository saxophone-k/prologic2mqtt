# prologic2mqtt

Bridge a **Hayward ProLogic PS4** pool controller to **Home Assistant** via MQTT.

Uses an Elfin EW11 WiFi–RS-485 adapter to tap the controller's RS-485 bus and
runs as a Docker container on any local server (TrueNAS, Raspberry Pi, NAS, etc.).
All pool state is published to your MQTT broker and auto-discovered by Home Assistant.

---

## What you get in Home Assistant

| Entity | Type | Description |
|--------|------|-------------|
| Pool Temperature | Sensor (°F/°C) | Live water temp (binary, 1 Hz) |
| Air Temperature | Sensor (°F/°C) | Panel air sensor |
| Spa Temperature | Sensor (°F/°C) | Spa water temp (display text) |
| Pool Setpoint | Sensor (°F/°C) | Pool heat setpoint |
| Salt Level | Sensor (ppm) | Salt chlorinator reading |
| Pool Chlorinator | Sensor (%) | SWG output % in pool mode |
| Spa Chlorinator | Sensor (%) | SWG output % in spa mode |
| Mode | Sensor | POOL / SPA / TRANSITION |
| Heat Pump Mode | Sensor | auto / off |
| Filter Pump | Binary sensor | Running / stopped |
| Spa Jets | Binary sensor | On / off |
| Super Chlorinate | Binary sensor | Active / inactive |
| Heater Active | Binary sensor | Relay energised (compressor running) |
| Spa Timer | Sensor | Spa auto-revert countdown (H:MM) |
| Jets Timer | Sensor | Jets auto-off countdown (H:MM) |
| Super Chlorinate Timer | Sensor | Boost-chlor remaining (H:MM) |
| Panel Clock | Sensor | Controller's internal clock |

> **Control (Phase 4):** Pool/Spa toggle, Filter Pump, Spa Jets, Heat Pump Auto/Manual, and
> Super Chlorinate will be added as switches in a future release. This version is read-only.

---

## Hardware required

| Item | Notes |
|------|-------|
| Hayward ProLogic PS4 (or PS8) | Tested on board G1-066008J-1, display 007-300-01 |
| [Elfin EW11](http://www.hi-flying.com/elfin-ew11) or EW11A | WiFi–RS-485 bridge. EW11 = 5–18 V, EW11A = 5–36 V. **Buy the EW11, not EW10 (RS-232).** |
| Home network with 2.4 GHz WiFi | The EW11 does not support 5 GHz |
| Docker host on the same LAN | TrueNAS SCALE, Raspberry Pi, Synology, etc. |
| MQTT broker | Mosquitto recommended. Must be reachable by both the Docker host and Home Assistant. |
| Home Assistant | With the MQTT integration enabled |

---

## Wiring the EW11 to the ProLogic panel

> ⚠️ **Power down the ProLogic panel before touching the wiring.**
> The remote rail is fused at 2 A on the board — still treat it with care.
> An optional inline 250–500 mA fuse on the RED tap is good insurance.

### Which connector to use

Look for the **Remote Display terminal block** — a green 4-screw connector in the
top-left area of the board labelled `RED 1 / BLK 2 / YEL 3 / GRN 4`.

> **Do not confuse this with the WIRELESS ANTENNA connector** (small black header nearby,
> carries red/orange/yellow/black wires — that is for the Aqua Pod wireless base, not your EW11).

### Terminal functions

| Panel terminal | Label | Function |
|----------------|-------|----------|
| 1 | RED | +10–12 V DC (power for the EW11) |
| 2 | BLK | RS-485 data |
| 3 | YEL | RS-485 data |
| 4 | GRN | Ground / common |

The EW11 kit includes an RJ45-to-terminal breakout with screw terminals labelled A / B / C / D.

### Connection table

| EW11 terminal | Function | → ProLogic terminal |
|---------------|----------|---------------------|
| C | + (power in) | RED (1) |
| D | − (ground) | GRN (4) |
| A | RS-485 data | BLK (2) |
| B | RS-485 data | YEL (3) |

### Data polarity note

If after wiring you see only garbage bytes (no clean `10 02 … 10 03` frames), **swap the two
data wires at one end** — connect A↔YEL(3) and B↔BLK(2) instead. This is harmless: RS-485
A/B polarity varies by manufacturer. The above table reflects the working orientation confirmed
on this panel.

### Power note

The EW11 is powered directly from the panel's 10.65 V remote rail (measured on this unit;
spec is 10–12 V). No separate power supply is needed. The EW11A accepts up to 36 V and works
equally well.

---

## EW11 configuration (web UI)

Connect to the EW11's web interface (default IP shown on the device label, or find it in your
router's DHCP table after it joins your WiFi).

### WiFi (Network settings)
| Setting | Value |
|---------|-------|
| Mode | **STA** (station — joins your existing WiFi) |
| SSID | your 2.4 GHz network name |
| Password | your WiFi password |

### Serial port settings
| Setting | Value |
|---------|-------|
| Baud rate | **19200** |
| Data bits | **8** |
| Parity | **None** |
| Stop bits | **2** |
| Protocol | **RS485** (not RS232, not Modbus) |

### Network / TCP settings
| Setting | Value |
|---------|-------|
| Work mode | **TCP Server** |
| Local port | **8899** (or any unused port — update `P2M_EW11_PORT` to match) |
| Buffer | Raw / transparent (no Modbus framing) |

Save and reboot the EW11. After it reconnects you should be able to `telnet <EW11_IP> 8899`
and see a stream of binary bytes — that is the RS-485 bus traffic.

### Assign a DHCP reservation

In your router, reserve a static IP for the EW11's MAC address so it never changes.
The default in this project is `192.168.107.61` — change `P2M_EW11_HOST` if yours differs.

---

## Deployment

### 1. MQTT broker

Make sure Mosquitto (or another broker) is running and reachable on your LAN.
Note its IP address — you will need it below.

### 2. Home Assistant — enable MQTT integration

In Home Assistant go to **Settings → Devices & Services → Add Integration → MQTT**.
Enter your broker's IP and port (default 1883). Leave discovery enabled (default).

### 3. Pull and run the container

Copy `docker-compose.yml` from this repository to your Docker host and edit two lines:

```yaml
P2M_EW11_HOST: "192.168.107.61"   # ← your EW11's reserved IP
P2M_MQTT_HOST: "192.168.1.x"      # ← your Mosquitto broker IP
```

Then run:

```bash
docker compose pull
docker compose up -d
```

Check logs to confirm connectivity:

```bash
docker compose logs -f
```

You should see:
```
Bridge running — EW11 192.168.x.x:8899  ->  MQTT 192.168.x.x:1883
State update: {'mode': 'POOL', 'filter_running': True, ...}
```

Within 30 seconds, all 17 entities appear automatically in Home Assistant under a
**Hayward ProLogic** device.

### Environment variables reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `P2M_EW11_HOST` | yes | `192.168.107.61` | EW11 IP address |
| `P2M_EW11_PORT` | no | `8899` | EW11 TCP port |
| `P2M_MQTT_HOST` | **yes** | — | MQTT broker IP or hostname |
| `P2M_MQTT_PORT` | no | `1883` | MQTT broker port |
| `P2M_MQTT_USERNAME` | no | — | MQTT username (if broker requires auth) |
| `P2M_MQTT_PASSWORD` | no | — | MQTT password |
| `P2M_MQTT_TOPIC_PREFIX` | no | `prologic2mqtt` | MQTT topic prefix |
| `P2M_LOG_LEVEL` | no | `info` | `debug` / `info` / `warning` / `error` |

---

## How it works

```
ProLogic PS4 panel
      │  RS-485 bus (19200 baud 8-N-2)
      │  Remote Display terminal block
      │
   Elfin EW11
      │  WiFi → TCP server on port 8899
      │
prologic2mqtt (Docker container)
      │  Parses DLE-STX framed RS-485 packets
      │  Decodes LED state, binary sensor frames, display text
      │
   Mosquitto MQTT broker
      │  MQTT Discovery → homeassistant/sensor|binary_sensor/prologic_*/config
      │
   Home Assistant
```

The bridge connects to the EW11 over TCP and reads the raw RS-485 stream. The
ProLogic controller broadcasts status frames at ~10 Hz (keepalive) and ~1 Hz (state).
The bridge decodes them in real time and publishes only when state changes.
Auto-reconnects if the EW11 goes offline. No polling — purely push-based.

---

## Compatibility

Tested on:
- Hayward **ProLogic PS4**, board `G1-066008J-1`, display `007-300-01` (datecode 1308)

The Goldline RS-485 protocol is shared across the ProLogic PS4, PS8, and AquaLogic
product lines. This bridge should work on PS8 units with no changes. Other Goldline /
Hayward AquaLogic variants likely work too — open an issue if you test one.

The EW11A (36 V input) is a drop-in replacement for the EW11 (18 V input). Both work.

---

## Protocol notes

A full reverse-engineered protocol specification for this PS4 unit is in
[`PROTOCOL.md`](PROTOCOL.md) (included in the `prologic-ha` development repository,
not shipped in this container image). Key facts:

- Frame: `10 02` [payload] `10 03` with `10 10` byte-stuffing
- Checksum: 16-bit sum of `10 02` + payload (excluding last 2 bytes), big-endian
- Temperatures in binary frames use `byte − 40` (short 8c) or `byte + 32` (long 8c)
- All countdown timers (spa, jets, super-chlor) are text-only — parsed from the
  rotating 40-character display screen

---

## Troubleshooting

**No entities appear in Home Assistant**
- Check that the MQTT integration is enabled and discovery is on
- Run `docker compose logs` — look for "MQTT Discovery published"
- Subscribe to `homeassistant/#` with MQTT Explorer to verify discovery messages arrive

**"Required environment variable missing: P2M_MQTT_HOST"**
- You forgot to set `P2M_MQTT_HOST` in `docker-compose.yml`

**Bridge connects but state never updates (all sensors unknown)**
- Check `docker compose logs` for "EW11" connection errors
- Verify the EW11 IP and port are correct
- Try `telnet <EW11_IP> 8899` from the Docker host — if it hangs, the EW11 is unreachable

**All sensors show stale / wrong values**
- Set `P2M_LOG_LEVEL: "debug"` and restart — raw frame counts and state changes appear in logs
- `bad_cs` count > 0 in logs means RS-485 noise — check wiring, try swapping BLK/YEL data wires

**Pool temperature jumps between two values**
- Normal: pool temp comes from two sources: the short 8c frame (every ~1 s) and the
  long 8c frame (every ~19 s, triggered by the Aqua Pod wireless base polling). Both
  should agree within 1–2 °F once the system is steady.

---

## License

MIT

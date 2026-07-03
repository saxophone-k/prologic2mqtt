# prologic2mqtt

Bridge a **Hayward ProLogic PS4** pool controller to **Home Assistant** via MQTT.

Uses an Elfin EW11 WiFi–RS-485 adapter to tap the controller's RS-485 bus and
runs as a Docker container on any local server (TrueNAS, Raspberry Pi, NAS, etc.).
All pool state is published to your MQTT broker and auto-discovered by Home Assistant
as sensors and controllable switches.

> **Vibe-coded with the help of AI.** This project was built through iterative
> reverse-engineering and testing on a real pool. It works on the hardware it was
> developed on. My ability to troubleshoot issues on different setups is limited —
> use it at your own risk, especially the control switches which drive real
> 240 V equipment and motorized valves.

---

## What you get in Home Assistant

### Sensors (11)

| Entity | Description |
|--------|-------------|
| Water Temperature | Live water temp — spa temp in Spa mode, pool temp otherwise |
| Air Temperature | Panel air sensor |
| Salt Level | Salt chlorinator reading (ppm) |
| Chlorinator Level | SWG output % — spa value in Spa mode, pool value otherwise |
| Mode | `POOL` / `SPA` / `TRANSITION` |
| Heat Pump Mode | `Auto Control` / `Manual Off` |
| Heat Pump Activity | `Heating` / `Off` (relay state, not just mode) |
| Spa Timer | Spa auto-revert countdown (e.g. `3h45m`) |
| Spa Jets Timer | Jets auto-off countdown |
| Super Chlorinate Timer | Boost-chlor remaining |
| Panel Clock | Controller's internal clock |

### Switches (5)

| Entity | What it does |
|--------|--------------|
| Spa Mode | Toggle between Pool mode and Spa mode |
| Filter Pump | Turn the main circulation pump on/off |
| Spa Jets | Turn the spa jets booster pump (AUX 1) on/off |
| Heat Pump | Auto Control (enabled) vs Manual Off |
| Super Chlorinate | Start/stop a super-chlorination boost cycle |

> ⚠️ **The control switches drive real equipment.** The Filter Pump runs a 1.5 HP motor,
> Spa Mode rotates motorized valves, and Heat Pump switches a 40 A heat pump compressor.
> Test with someone physically present at the panel the first time.

---

## Lovelace dashboard

A ready-to-use phone dashboard card is included in the repo as `lovelace_pool.yaml`.

**Requires:** [Mushroom Cards](https://github.com/piitaya/lovelace-mushroom) and
[button-card](https://github.com/custom-cards/lovelace-button-card) — both installable via HACS.

**Layout:**
- Mode chip (POOL / SPA / TRANSITION) + panel clock
- 4 large control buttons in a 2×2 grid (Spa Mode, Spa Jets, Heat Pump, Filter Pump)
  - Active timers appear *inside* the button when running
  - Heat Pump has 3-color logic: grey = off, green = Auto/standby, orange = actively heating
- 4 compact sensor readouts (Water °F, Air °C, Salt ppm, Chlorinator %)
- Super Chlorinate full-width button at the bottom with its countdown timer inside

To add it: **Dashboard → Edit → Add Card → Manual** → paste the YAML.

Entity IDs in HA use the `hayward_prologic_` prefix. If any card shows "Entity not found",
check the exact ID under **Settings → Devices → Hayward ProLogic**.

---

## Hardware required

| Item | Notes |
|------|-------|
| Hayward ProLogic PS4 (or PS8) | Tested on board G1-066008J-1, display 007-300-01 |
| [Elfin EW11](http://www.hi-flying.com/elfin-ew11) or EW11A | WiFi–RS-485 bridge. **Buy the EW11, not EW10 (RS-232).** EW11 = 5–18 V input, EW11A = 5–36 V. |
| Home network with 2.4 GHz WiFi | The EW11 does not support 5 GHz |
| Docker host on the same LAN | TrueNAS SCALE, Raspberry Pi, Synology, etc. |
| MQTT broker | Mosquitto recommended |
| Home Assistant | With the MQTT integration enabled |

---

## Wiring the EW11 to the ProLogic panel

> ⚠️ **Power down the ProLogic panel before touching the wiring.**
> The remote rail is fused at 2 A on the board.
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

The EW11 is powered directly from the panel's remote rail (~10.6 V measured on this unit).
No separate power supply is needed. The EW11A accepts up to 36 V and works equally well.

---

## EW11 configuration (web UI)

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

Save and reboot. After reconnect, `telnet <EW11_IP> 8899` should show a stream of binary bytes.

### Assign a DHCP reservation

Reserve a static IP for the EW11's MAC address so it never changes.
The default in this project is `192.168.107.61` — change `P2M_EW11_HOST` if yours differs.

### Install the Lua script (required for control)

By default the EW11 only **receives** data — it does not transmit on RS-485.
Sending control commands requires a Lua script loaded into the EW11 via
**IOTService** (Hi-Flying's Windows configuration tool). The script handles
the half-duplex direction switch and timing so commands are queued and sent
immediately after a Keep Alive frame, which is the only safe transmission window.

**Without this script, all sensors work but the switches will have no effect.**

#### Step 1 — Get IOTService

Download the IOTService Windows app from the Hi-Flying FTP:
`http://ftp.hi-flying.com:9000/IOTService/`
(download is slow — give it a minute)

#### Step 2 — Connect PC to EW11 temporarily in AP mode

If your EW11 is already on your LAN you can skip to Step 3.
Otherwise, connect your PC's WiFi to the EW11's own hotspot (`EW11_????`, open network)
and use IOTService to configure the WiFi → STA mode first.

#### Step 3 — Load the script

1. With your PC on the same LAN as the EW11, open IOTService — the device should appear automatically.
2. Double-click the device → **Edit** → **Detail** → **Edit Script**.
3. Set **UART Gap Time to 10 ms** (critical — this is the inter-frame window the script relies on).
4. Click **Import Script** and select the `EW11_script.txt` file from this repo's `docs/` folder.
5. Confirm the import and reboot the EW11.

#### The script

The script is included in this repo at `docs/EW11_script.txt`. It was adapted from
[smith288/pool-controller](https://github.com/smith288/pool-controller). Key behaviour:

- Keep Alive frames (`10 02 01 01 …`) are filtered out and not forwarded to the TCP client
  (they would flood your bridge with noise). The bridge generates its own timing from the stream.
- Frames received from the TCP socket (i.e. commands from this bridge) are queued and sent
  on the RS-485 bus immediately after the next Keep Alive — the only gap the half-duplex bus
  allows for writing.
- The script also handles the UNLOCK / HOLD sequence required by some panel variants.

---

## Deployment

### 1. MQTT broker

Make sure Mosquitto (or another broker) is running and reachable on your LAN.

### 2. Home Assistant — enable MQTT integration

**Settings → Devices & Services → Add Integration → MQTT.**
Enter your broker's IP and port (default 1883). Leave discovery enabled.

### 3. Pull and run the container

Copy `docker-compose.yml` to your Docker host and edit two lines:

```yaml
P2M_EW11_HOST: "192.168.107.61"   # ← your EW11's reserved IP
P2M_MQTT_HOST: "192.168.1.x"      # ← your MQTT broker IP
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
MQTT Discovery published — 11 sensors + 5 switches
State update: {'mode': 'POOL', 'filter_running': True, ...}
```

All entities appear automatically in Home Assistant under a **Hayward ProLogic** device.

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
      │  Sends Aqua Pod-format command frames (00 8c) for control
      │
   Mosquitto MQTT broker
      │  MQTT Discovery → homeassistant/sensor|switch/prologic_*/config
      │
   Home Assistant
```

The bridge connects to the EW11 over TCP and reads the raw RS-485 stream. The
ProLogic controller broadcasts status frames continuously. The bridge decodes them
in real time and publishes only when state changes. Control commands are sent as
Aqua Pod-format wireless key frames (`00 8c`) — the same format the Aqua Pod
wireless remote uses, verified working on hardware. Auto-reconnects if the EW11
goes offline. No polling — purely push-based.

---

## Known limitations

- **Heat setpoints** are not exposed as sensors. They appear on the RS-485 bus only
  when navigating the Settings menu — they are not continuously broadcast. A future
  "panel emulator" feature (sending MENU/+/-/←/→ button presses and parsing the
  display response) would enable reading and setting them.
- **Timer resolution** is one display rotation cycle (~20–40 s). Timers (spa countdown,
  jets, super-chlor) are parsed from the rotating 40-character text display, so they
  update only when that screen appears in the rotation.
- **Spa temperature** updates on the same display-rotation cadence (not binary, unlike
  pool and air temp which come from a 1 Hz binary frame).
- **Single TCP client** — the EW11 only accepts one connection at a time. Running another
  tool against it simultaneously will starve this bridge.

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
`PROTOCOL.md` (in the development repository). Key facts:

- Frame: `10 02` [payload] `10 03` with `10 10` byte-stuffing
- Checksum: 16-bit sum of `10 02` + payload (excluding last 2 bytes), big-endian
- LED_STATE (`01 02`): 4 on-bytes + 4 flashing-bytes; bit map confirmed for this unit
- Short 8c frame: temperature bytes use `byte − 40`; clock in body[2–4]
- Long 8c frame: `body[20] × 100 = salt PPM`; other bytes appear static
- Control: `00 8c 01 [key4] [key4] 00` with standard checksum; double-send 200 ms apart

---

## Troubleshooting

**No entities appear in Home Assistant**
- Check that the MQTT integration is enabled and discovery is on
- Run `docker compose logs` — look for "MQTT Discovery published"
- Subscribe to `homeassistant/#` with MQTT Explorer to verify discovery messages arrive

**"Required environment variable missing: P2M_MQTT_HOST"**
- You forgot to set `P2M_MQTT_HOST` in `docker-compose.yml`

**Bridge connects but state never updates**
- Check `docker compose logs` for EW11 connection errors
- Verify the EW11 IP and port are correct
- Try `telnet <EW11_IP> 8899` from the Docker host — if it hangs, the EW11 is unreachable

**Sensors show stale / wrong values**
- Set `P2M_LOG_LEVEL: "debug"` and restart
- `bad_cs` count > 0 in logs means RS-485 noise — check wiring, try swapping BLK/YEL

**A control switch doesn't respond or bounces back**
- The bridge uses read-before-write and verifies LED state after each command
- If the LED doesn't confirm within ~20 s it retries once; check logs for "FAILED after retry"
- For Pool/Spa toggle: the 35-second valve transition is normal — the switch state reflects
  the confirmed mode, not the optimistic toggle

---

## License

MIT

# ESS BLE CLI

> Interactive Python CLI for the **ESS Smart Sensor** — a BLE‑connected
> condition‑monitoring device built around the **STM32WBA5MMG** with an
> **LSM6DSV** IMU + on‑device **FFT analysis** and an **STS4x** temperature
> sensor.

The CLI lets you:

- 🔍 Continuously scan & persistently track ESS devices in JSON
- 🔗 Connect to a known device by MAC
- 📋 Inspect GATT services & characteristics (filtered to ESS UUIDs `0000fe*`)
- 🎛 Configure each sensor (schema‑driven, partial updates OK)
- 📈 Live‑plot data with multi‑series support (`xyz` accel, FFT bar chart, scalar traces)
- 💾 Record long‑format CSV captures, per sensor, to `measurements/`
- 🛡 State‑aware prompt with autocomplete, mutually‑exclusive states, graceful shutdown

The design is **modular and schema‑driven** — adding a new sensor is a single new file in `sensors/`, no CLI changes.

---

## 📦 Requirements

- **Python 3.10+** (tested on 3.13)
- Windows 10/11, Linux, or macOS
- A working BLE adapter (built‑in or USB)
- Your ESS device, advertising as `ESS_EX3`

> **Windows users:** Microsoft Store Python works fine. Linux: install `bluez`. macOS: grant Bluetooth permission to your terminal.

---

## 🚀 Installation

### 1. Clone the repository

```bash
git clone https://github.com/<your-org>/ess-cli.git
cd ess-cli
```

### 2. Create a virtual environment (recommended)

```bash
python -m venv .venv

# Windows
.\.venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate
```

### 3. Install dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 4. Run

```bash
python -m esbl_vsm1-cli
```

You should see:

```
ESS BLE CLI — type `help` for commands. TAB to autocomplete.
(idle) ess>
```

---

## 🧭 Command Workflow

The CLI is a **state machine**:

```
   idle ──► scanning ──► (idle) ──► connecting ──► connected ──► (idle)
                                       ▲                ▲
                                       │                │
                                       └── disconnect ──┘
```

Scanning and a live BLE connection are **mutually exclusive** — the prompt always reflects the current state:

```
(idle) ess>
(scanning) ess>
(connecting) ess>
(connected 00:80:E1:2A:48:89) ess>
```

### Typical session

```text
(idle) ess> scan
🔍 Scanning started in the background.

(scanning) ess> list_devices
MAC                  NAME                  RSSI  seen
00:80:E1:2A:48:89    ESS_EX3                -54   46
00:80:E1:2A:3E:27    ESS_EX3                -52   42

(scanning) ess> connect -mac 00:80:E1:2A:48:89
🔗 Connecting to 00:80:E1:2A:48:89 ...
✅ Connected to 00:80:E1:2A:48:89

(connected ...) ess> list_characteristics
🔑 ESS Characteristics on 00:80:E1:2A:48:89: ...

(connected ...) ess> subscribe -module imu
✅ Subscribed to 'imu' — 2 live plot(s) opened.

(connected ...) ess> configure -module imu -low_scale 8g -mode high
✅ Configured LSM6DSV IMU/FFT: ...
ℹ️  Subscription 'imu' refreshed — plot titles updated.

(connected ...) ess> record -module imu -out shaker_run3.csv
🔴 Recording 'imu' → measurements/shaker_run3.csv

# ...run your test...

(connected ...) ess> stop_record -module imu
⏹  Stopped recording 'imu': 124 frames, 1240 rows, 24.8 s

(connected ...) ess> disconnect
👋 Disconnected.
```

---

## 📚 Command Reference

> Press **TAB** at any point for context‑aware autocompletion of commands, flags, sensor keys, MAC addresses, and parameter values.

### Discovery & connection

| Command                     | Notes                                                     |
| --------------------------- | --------------------------------------------------------- |
| `scan`                      | Start background scanning. Updates `ess_devices.json`.    |
| `stop`                      | Stop background scanning.                                 |
| `list_devices`              | List ESS devices saved during scanning.                   |
| `connect -mac <ADDRESS>`    | Connect to a known device. **Scanning is auto-stopped.**  |
| `disconnect`                | Disconnect. Auto-closes subscriptions and CSV recordings. |
| `clear`                     | Wipe `ess_devices.json`.                                  |

### GATT inspection (must be `connected`)

| Command                                  | Notes                                                |
| ---------------------------------------- | ---------------------------------------------------- |
| `list_services`                          | All GATT services on the connected device.           |
| `list_characteristics`                   | Only ESS (`0000fe*`) characteristics with properties.|
| `list_characteristics --service <UUID>`  | Filter to a specific service.                        |

### Subscriptions (must be `connected`)

Each sensor module decides what plots open. The CLI doesn't hard‑code anything.

| Command                          | Behavior                                                                       |
| -------------------------------- | ------------------------------------------------------------------------------ |
| `subscribe -module <name>`       | Read sensor config + start BLE notifications + open live plot(s).             |
| `unsubscribe -module <name>`     | Stop notifications, close plots, auto-close any active CSV recording.         |

Closing the matplotlib window for a subscription auto‑triggers an `unsubscribe`.

### Configuration (schema‑driven)

Each sensor exposes its own editable `PARAMS`. Partial updates are supported — current values are read first and merged.

| Sensor | Flags                                                                                                                |
| ------ | -------------------------------------------------------------------------------------------------------------------- |
| `temp` | `-freq <1..10>`                                                                                                      |
| `imu`  | `-low_scale <2g\|4g\|8g\|16g>` `-high_scale <32g\|64g\|128g\|256g>` `-mode <low\|high>`                               |
| `fft`  | `-axis <x\|y\|z>` `-mode <low\|high>` — `low_scale` and `high_scale` are preserved (set via `configure -module imu`) |

Configuring a shared‑config sensor (e.g. `imu` ↔ `fft` share `fe51`) automatically refreshes plot titles on **both** active subscriptions.

# Maintenance & Device Management Commands

In addition to sensor streaming, recording, and configuration, the ESS BLE CLI provides a set of maintenance commands for device deployment, calibration, and firmware management.

---

# ⏰ Device Provisioning

Configure the RTC wake-up schedule used by ultra-low-power deployed sensors.

Provisioning is typically performed during installation or commissioning and defines how often the sensor wakes from low-power mode to perform measurements and communications.

## Command

```text
provision <RTC_OPTION>
```

## Available Options

```text
RTC_DISABLRD
RTC_30_SECS
RTC_1_MIN
RTC_15_MIN
RTC_30_MIN
RTC_1_HOUR
RTC_4_HOURS
RTC_8_HOURS
RTC_12_HOURS
RTC_1_DAY
```

## Example

```text
(connected 00:80:E1:2A:48:89) ess> provision RTC_1_HOUR

Sensor Provisioning Requested

✅ Successful
```

## Example Options

```text
provision RTC_30_SECS
provision RTC_15_MIN
provision RTC_1_HOUR
provision RTC_1_DAY
```

## Notes

- Used primarily before field deployment.
- Configures the sensor's RTC wake-up interval.
- Device confirms provisioning by echoing the configuration packet back to the CLI.
- Provisioning does not reboot the device.

---

# 🛠 Sensor Calibration

Perform installation calibration for the IMU.

The calibration routine validates:

- Z-axis alignment
- Gravity vector magnitude
- Sensor stability (jitter detection)

When calibration succeeds, the device calculates and stores the sensor installation rotation angle.

## Commands

### Default calibration

```text
calibrate -module imu
```

### Custom thresholds

```text
calibrate -module imu \
          -z_offset <mg> \
          -mag_xy <mg> \
          -jitter <mg>
```

## Example

```text
(connected 00:80:E1:2A:48:89) ess> calibrate -module imu

🛠 Calibration requested:
     Module : imu

👋 Sensor Calibration Requested — waiting for result...

✅ Accepted

🎉 Calibration SUCCESS — theta ≈ +23.4°
     (+0.409 rad)

✅ Done — rotation theta = +23.40°
```

## Example with custom thresholds

```text
(connected 00:80:E1:2A:48:89) ess> calibrate -module imu \
                                             -z_offset 100 \
                                             -mag_xy 150 \
                                             -jitter 50
```

## Possible Calibration Responses

### Z-axis error

```text
⚠️ Z-axis misalignment: 124 mg exceeds threshold
```

### XY-plane error

```text
⚠️ XY-plane misalignment: 183 mg off from 1000 mg
```

### Motion detected

```text
⚠️ Jitter / movement detected — hold the sensor still
```

### Timeout

```text
⏱ Calibration TIMED OUT.
```

## Notes

- Sensor should remain stationary during calibration.
- The reported angle is used by firmware to compensate installation orientation.
- Calibration thresholds are specified in **mg**.
- A successful calibration returns both radians and degrees.

---

# 🔄 Firmware Update (FOTA)

Perform a Firmware Update Over The Air (FOTA) using a compiled application image.

The CLI automatically:

1. Validates the firmware image.
2. Computes the image CRC32.
3. Sends firmware metadata.
4. Streams the image page-by-page.
5. Collects packet acknowledgements.
6. Reports transfer statistics.

## Command

```text
update -module fota -file <firmware.bin>
```

## Example

```text
(connected 00:80:E1:2A:48:89) ess> update -module fota -file firmware.bin
```

## Example Output

```text
📦 FOTA file selected:

     File              : firmware.bin
     Path              : firmware.bin
     Version           : @v1.2.3
     Original size     : 169947 B
     Image CRC32       : 0x215AB499
     Flash page size   : 8192 B
     Pages             : 21
     Page padding      : 2085 B
     Payload           : 224 B/packet
     Packets/page      : 37
     Total packets     : 777

FOTA Accepted. Proceeding...

🚀 Sending FOTA: 777/777 packets (100.0%)

✅ FOTA file streaming complete:

     FW size           : 169947 B
     Image CRC32       : 0x215AB499
     Flash pages       : 21
     Sent              : 777/777
     ACK'd unique      : 37
     Duplicates        : 740
     Bad-length        : 0
     Throughput        : 25.62 KB/s firmware
     Est. 500 KB       : 20.0 s
     Missing           : 0 🎉 all packets ACK'd
```

## Transfer Characteristics

### Firmware Image

```text
.bin application image
```

### Flash Page Size

```text
8192 bytes
```

### BLE Payload Size

```text
224 bytes
```

### Transport Packet Size

```text
240 bytes
```

### Integrity Protection

```text
CRC32 per packet
CRC32 for complete image
```

## Notes

- Only `.bin` files are supported.
- Firmware is automatically split into flash pages.
- Each page is transported using 37 BLE packets.
- The device validates packet CRCs before writing.
- Firmware activation and installation behavior is firmware-dependent.
- Transfer statistics are displayed when streaming completes.

---

# Maintenance Workflow Example

## Provision a device

```text
(connected ...) ess> provision RTC_4_HOURS

Sensor Provisioning Requested

✅ Successful
```

## Calibrate installation

```text
(connected ...) ess> calibrate -module imu

🎉 Calibration SUCCESS
✅ Done — rotation theta = +18.7°
```

## Update firmware

```text
(connected ...) ess> update -module fota -file ess_fw_v1_2_3.bin

✅ FOTA file streaming complete
```

These commands are intended for deployment, commissioning, maintenance, and firmware lifecycle management of ESS Smart Sensors.


### CSV recording (must be `subscribed`)

Long‑format CSVs are written to `measurements/` (auto‑created).

| Command                                          | Behavior                                          |
| ------------------------------------------------ | ------------------------------------------------- |
| `record -module <name> [-out <file.csv>]`        | Start CSV recording for an active subscription.   |
| `stop_record -module <name>`                     | Stop CSV recording + close file.                  |

If `-out` is omitted, the file is auto‑named `ess_<module>_<UTC>.csv`.
Absolute paths in `-out` are honored as‑is (escape hatch for advanced setups).

| Module | CSV columns                                                                                                                           |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------- |
| `temp` | `t_host_iso, t_host_ns, channel, temperature_c`                                                                                       |
| `imu`  | `t_host_iso, t_host_ns, channel, x, y, z` — single file, multiple channels distinguished by the `channel` column                      |
| `fft`  | `t_host_iso, t_host_ns, channel, axis, bin_index, freq_hz, magnitude, rms, crest, kurtosis, vel_rms, disp_rms` — **50 rows per frame** |

The long format makes pandas analysis trivial:

```python
import pandas as pd
df = pd.read_csv("measurements/ess_fft_20260630T...csv")

# Per-frame RMS over time
rms = df.groupby("t_host_ns")["rms"].first()

# Spectrum at a single timestamp
frame = df[df.t_host_ns == df.t_host_ns.iloc[0]]
```

### Misc

| Command | Notes                       |
| ------- | --------------------------- |
| `help`  | Show the command reference. |
| `exit`  | Graceful shutdown.          |

---

## 🏗 Project Layout

```
ess_cli/
├── __main__.py              # python -m ess_cli entrypoint
├── main.py                  # async main + signal handlers
├── config.py                # constants: filter bytes, paths, UUID prefix
├── state.py                 # AppCtx singleton + State enum
│
├── ble/
│   ├── adv.py               # advertisement reconstruction + ESS filter
│   └── scanner.py           # background continuous scan
│
├── sensors/                 # one module per sensor
│   ├── registry.py          # SENSOR_REGISTRY
│   ├── sts4x.py             # STS4x temperature
│   ├── imu.py               # LSM6DSV IMU + accel streams
│   └── fft.py               # FFT spectrum + scalars
│
├── sinks/
│   └── csv_sink.py          # generic CSV sink (schema from sensor module)
│
├── plotting.py              # LivePlot (time series) + LiveBarPlot (spectrum)
│
└── cli/
    ├── repl.py              # interactive loop + dispatch
    ├── prompt.py            # state-aware prompt
    ├── completer.py         # context-aware autocompletion
    └── commands/
        ├── base.py          # require_connected, format_props
        ├── scan.py          # scan / stop / list_devices / clear
        ├── connection.py    # connect / disconnect
        ├── gatt.py          # list_services / list_characteristics
        ├── sensors.py       # subscribe / unsubscribe / configure / record / stop_record
        └── help.py
```

### Design rules

- **Modules own protocol & policy.** Each sensor module declares `PARAMS`, `DATA_CHANNELS`, codecs, `format_config_title`, `channel_title_suffix`, `CSV_HEADER`, and `csv_rows()`. The CLI never special‑cases a sensor.
- **CLI is sensor‑agnostic.** Adding a new sensor = drop a file in `sensors/` + register it in `registry.py`.
- **Producers don't block.** BLE callbacks fan out to live plots and (optionally) CSV sinks with O(1) work; heavy I/O lives in worker subprocesses.
- **Plots are process‑isolated.** Matplotlib runs in subprocesses to keep `asyncio` + `prompt_toolkit` responsive.

---

## 🛠 Configuration & Files

| File                           | Purpose                                                            |
| ------------------------------ | ------------------------------------------------------------------ |
| `ess_devices.json`             | Known ESS devices accumulated during `scan`. Manually clearable via `clear`. |
| `measurements/*.csv`           | Long-format CSV captures, one per `record` session.                |
| `.vscode/settings.json`        | Optional — pins the right Python interpreter for VS Code users.    |

---

## 🧪 Development

### Linting

```bash
python -m pylint ess_cli
python -m flake8 ess_cli
```

### Recommended VS Code settings

If `bleak` shows as "unresolved" but the script runs, your editor's interpreter doesn't match the runtime one. Fix it once:

`.vscode/settings.json`

```json
{
  "python.defaultInterpreterPath": ".venv/Scripts/python.exe",
  "python.analysis.extraPaths": ["./"],
  "python.languageServer": "Pylance"
}
```

Then **Ctrl+Shift+P → Developer: Reload Window**.

---

## 🐛 Troubleshooting

| Symptom                                            | Likely cause / fix                                                                                                                       |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `Unable to import 'bleak'` in editor               | Interpreter mismatch — select the venv Python in **Python: Select Interpreter**, reload window.                                          |
| Plot window opens but stays empty                  | Matplotlib `FuncAnimation` was garbage‑collected — already fixed in `plotting.py` via a module‑level anchor list.                        |
| `subscribe` succeeds but mobile app shows old values | Write reached firmware but the handler is a no‑op, OR the readback comes from Windows BLE cache. Cross-check with nRF Connect / mobile app. |
| Console spam: "Press ENTER to continue..."         | A scan callback raised. Fixed by length-guarding `is_ess_device` + `try/except` in the callback + quiet asyncio loop handler.            |
| `bleak.exc.BleakError: GATT_REQ_NOT_SUPPORTED`     | The characteristic is `Write‑without‑response` only. Set `response=False` in `cmd_configure`.                                            |

---

## 📜 License

MIT — see `LICENSE` if present, otherwise feel free to adapt.

---

## 🙏 Acknowledgements

- [bleak](https://github.com/hbldh/bleak) — async BLE that just works across platforms
- [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit) — the magical async REPL
- [matplotlib](https://matplotlib.org/) — for the live plots
- The STM32WBA + LSM6DSV community

---

> Built with 🛠 for embedded condition‑monitoring development.

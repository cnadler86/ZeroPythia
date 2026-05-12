# ZeroPythia

**Three-phase zero-feed-in battery controller with feedforward + feedback regulation for single-phase inverters.**

ZeroPythia keeps your home from exporting energy to the grid by continuously controlling a Zendure SolarFlow battery storage unit. It reads all three grid phases via a Shelly energy meter and computes the battery setpoint in real time. An optional integration with [GridPythia](https://github.com/cnadler86/EOS2) lets an external energy optimizer schedule charge and discharge slots instead.

---

## How it works

ZeroPythia uses a hybrid control strategy across three phases:

- **Feedforward phases** (the two phases without the battery inverter): a P-controller predicts how much battery power is needed based on the current load on those phases. Oscillation detectors prevent the controller from reacting to short cyclic loads like washing machines.
- **Feedback phase** (the phase where the battery inverter is connected): a P-controller closes the loop by measuring actual grid flow on that phase and correcting the setpoint accordingly.

The combination means the controller can react quickly to load changes on all phases, while the feedback loop on the battery phase corrects residual errors and prevents sustained feed-in.

---

## Startup Scripts

There are three entry points, each for a different use case:

| Script | Purpose |
| --- | --- |
| `utils/start_dashboard_server.py` | Full operation with real Shelly + Zendure hardware |
| `utils/start_dashboard_mock.py` | Local testing without any real devices |
| `utils/start_gridpythia_bridge.py` | GridPythia plan executor only, no dashboard |

---

## Quick Start

**Requirements:** Python 3.11 or newer.

### 1. Install dependencies

```bash
pip install -e .
```

### 2. Start the dashboard (real hardware)

```bash
python utils/start_dashboard_server.py --shelly 192.168.178.77 --zendure 192.168.178.140
```

Then open **http://127.0.0.1:8765** in your browser.

For LAN access from other devices:

```bash
python utils/start_dashboard_server.py --shelly 192.168.178.77 --zendure 192.168.178.140 --host 0.0.0.0
```

### 3. All CLI options for the dashboard server

```text
--shelly IP           Shelly 3EM IP address          (default: 192.168.178.77)
--zendure IP          Zendure SolarFlow IP address    (default: 192.168.178.140)
--host HOST           Server bind address             (default: 127.0.0.1)
--port PORT           HTTP port                       (default: 8765)
--max-output W        Maximum battery discharge       (default: 800 W)
--min-discharge W     Minimum battery output          (default: 20 W)
--control-interval S  Control cycle interval          (default: 3.0 s)
--initial-mode        idle | zero_feed                (default: idle)
--mqtt-broker URL     MQTT broker URL for auto mode   (default: mqtt://localhost:1883)
--device-id ID        Inverter device ID              (default: SF800Pro)
--auto                Start immediately in AUTO mode
--verbose             Enable debug logging
```

### 4. Start with AUTO mode (GridPythia integration)

If a GridPythia MQTT broker is available, the controller can follow an optimized schedule automatically:

```bash
python utils/start_dashboard_server.py \
  --shelly 192.168.178.77 \
  --zendure 192.168.178.140 \
  --auto \
  --mqtt-broker mqtt://192.168.1.5:1883 \
  --device-id SF800Pro
```

AUTO mode can also be enabled and disabled at runtime via the dashboard UI.

---

## Supported Hardware

### Grid Meter

| Device | Notes |
| --- | --- |
| **Shelly 3EM** (Gen1) | Tested; uses `/status` endpoint |
| **Shelly Pro 3EM** (Gen2) | Tested; uses `/rpc/EM.GetStatus` endpoint |

The generation is auto-detected on first connection. No manual configuration needed.

### Battery / Inverter

| Model | Max Discharge | Max Charge | Min Output |
| --- | --- | --- | --- |
| **Zendure SolarFlow 800 Pro** | 800 W | 1000 W | 20 W |
| **Zendure SolarFlow 800 Plus** | 800 W | 1000 W | 20 W |

Other Zendure models may work electrically but are not officially supported — hardware limits and safe operation have only been verified for the models above. If you add a new model, add its limits to `clients/zendure/models.py` under `MODEL_LIMITS`.

---

## GridPythia Bridge

The GridPythia Bridge connects ZeroPythia to an external energy management system ([GridPythia / EOS2](https://github.com/cnadler86/EOS2)) via MQTT.

> **Important:** The bridge does **not** run a zero-feed-in control loop. It simply executes the schedule that GridPythia publishes.

### What it does

**1. Status reporting** (every 60 s by default)

Reads the current SoC from the Zendure device and publishes it to GridPythia:

```text
Topic:   gridpythia/inverters/{device_id}/status
Payload: {"soc": 63.5, "mode": 2}
```

This keeps the optimizer informed about the real battery state.

**2. Plan execution** (every 60 s by default)

Subscribes to the GridPythia optimization plan:

```text
Topic:   gridpythia/inverters/{device_id}/plan
```

Each plan slot maps to a battery command:

| Plan mode | Battery command |
| --- | --- |
| `IDLE` | Stop / standby |
| `DISCHARGE` | Start discharge at plan power |
| `DISCHARGE_ZERO_FEED_IN` | Start discharge at plan power |
| `AC_CHARGE` | Start charge at plan power |
| No plan / stale plan | Stop (fallback, safe default) |

A plan is considered stale 30 minutes after the last slot has ended. The bridge then stops the battery until a new plan arrives.

### Starting the bridge

```bash
python utils/start_gridpythia_bridge.py \
  --zendure 192.168.178.140 \
  --device-id SF800Pro \
  --mqtt-broker mqtt://localhost:1883
```

All CLI options:

```text
--zendure IP          Zendure SolarFlow IP address    (default: 192.168.178.140)
--device-id ID        Device ID as in GridPythia      (default: SF800Pro)
--mqtt-broker URL     MQTT broker URL                 (default: mqtt://localhost:1883)
--topic-prefix STR    MQTT topic prefix               (default: gridpythia)
--status-interval S   Seconds between reports         (default: 60.0)
--verbose             Enable debug logging
```

---

## Configuration

The main configuration file is:

```text
config/zerofeed.yaml
```

The file is read on startup and can be updated at runtime via the dashboard. Comments in the YAML are preserved across saves.

### Top-level keys

| Key | Default | Description |
| --- | --- | --- |
| `language` | `en` | Dashboard UI language (`en` or `de`) |
| `control_phase` | `B` | Phase with the battery inverter (`A`, `B`, or `C`) |
| `target_power_w` | `1.0` | Desired total grid draw in W. A small positive value (e.g. 1–5 W) avoids accidental feed-in |
| `min_output_w` | `20` | Minimum battery output in W (hardware lower limit) |
| `max_output_w` | `800` | Maximum battery output in W (hardware upper limit) |
| `control_interval_s` | `3.0` | How often the controller computes a new setpoint |
| `sampling_interval_s` | `1.0` | How often grid power is sampled |
| `battery_dead_time_s` | `1.1` | Time from setpoint command until battery responds at the meter |
| `battery_pt1_tau_s` | `0.5` | PT1 time constant for battery ramp-up model |
| `watchdog_cycles` | `3` | Number of consecutive control cycles before the watchdog triggers |
| `watchdog_threshold_w` | `-10.0` | Feed-in threshold in W (negative = export to grid). Must be negative |

### Per-phase settings (`phases.A`, `phases.B`, `phases.C`)

Each phase has a `role`:

- `feedforward` — open-loop steering (phases without battery inverter)
- `feedback` — closed-loop regulation (the battery phase, must match `control_phase`)

Key tuning parameters:

| Key | Applies to | Description |
| --- | --- | --- |
| `kp` | feedforward | P-gain. `1.0` = full compensation of grid draw on this phase |
| `kp_draw` | feedback | P-gain when drawing from grid (conservative) |
| `kp_feed_in` | feedback | P-gain when feeding into grid (more aggressive pull-back) |
| `kp_hysteresis` | both | Damped gain inside the hysteresis band |
| `hysteresis_w` | both | Width of the hysteresis band in W |
| `feedback_enabled` | feedback | `false` = pure feedforward mode (useful for testing) |

### Oscillation detection

Each phase has two optional detectors under `phases.X.osc`:

- **`holder`** — catches fast short-cycle oscillations (e.g. a fan cycling every few seconds). Configured with `threshold`, `min_period`, `max_period`, `period_variance`, `time_threshold`, `min_rising_count`.
- **`predictor`** — detects periodic loads with a known cycle time (e.g. washing machine, dishwasher). Adds a `reaction_time` look-ahead so the battery output is reduced just before the expected load peak.

Either detector can be disabled by setting it to `null` in the YAML.

---

## Development & Testing

### Run tests

```bash
pytest
```

### Test without real hardware (mock mode)

```bash
python utils/start_dashboard_mock.py
```

The mock simulates a Zendure battery and a three-phase grid meter with realistic oscillating loads on phases A and C. Useful for testing the controller logic, dashboard UI, and oscillation detection without any physical devices.

Mock CLI options:

```text
--load-a W    Phase A base load  (default: 150 W)
--load-b W    Phase B base load  (default: 250 W)
--load-c W    Phase C base load  (default: 100 W)
--port PORT   HTTP port          (default: 8765)
--verbose     Enable debug logging
```

### Code quality

```bash
ruff check .
```

---

## License and Compliance

This project is licensed under:

- PolyForm Noncommercial License 1.0.0

Files in this repository:

- License text: [LICENSE.md](LICENSE.md)
- Required notice line(s): [NOTICE](NOTICE)

If you distribute this software (modified or unmodified), you must pass on:

- the PolyForm license text (or the official PolyForm URL), and
- all plain-text lines beginning with Required Notice:

Commercial use is not permitted under this license.
For commercial licensing, contact the licensor.

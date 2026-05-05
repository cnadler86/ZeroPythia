"""Dashboard server with mock hardware – for local testing without Shelly/Zendure.

Uses:
  - SolarFlowAsyncMockClient  (simulated battery with realistic timing)
  - MockGridMeter              (constant load with configurable values)

Starts dashboard at http://localhost:8765/
V4 settings are stored in config/zerofeed_v4.yaml.

Usage:
    python utils/start_dashboard_mock.py
    python utils/start_dashboard_mock.py --load-a 150 --load-b 200 --load-c 80
    python utils/start_dashboard_mock.py --port 8765 --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import random
import time
from enum import Enum
from pathlib import Path
from typing import Optional

import uvicorn

from clients.zendure.mock.async_mock_client import SolarFlowAsyncMockClient
from src.config.zerofeed_v4 import ZeroFeedV4Config
from src.controller.zerofeed_v4_regulator import ZeroFeedV4Regulator
from src.dashboard.models import DeviceMode
from src.dashboard.runtime import ControlRuntime
from src.dashboard.server import create_app

LOG = logging.getLogger("start_dashboard_mock")

# YAML config path for V4 (relative to project root)
_V4_CONFIG = Path("config") / "zerofeed_v4.yaml"


# ── Mock Grid Meter ───────────────────────────────────────────────────────────


# ── Oscillating load simulator ────────────────────────────────────────────────


class _PhaseState(Enum):
    STATIC = "static"
    OSCILLATING = "oscillating"


class OscillatingLoad:
    """Simulates a realistic load that alternates between static and oscillating phases.

    In STATIC phase:  constant value (random level ±30 % of base) + Gaussian noise.
    In OSCILLATING phase: rectified sine with random frequency + noise.
    Transitions happen after a random duration independently per instance.
    """

    def __init__(
        self,
        base_w: float,
        *,
        osc_amplitude_w: float = 80.0,
        osc_period_range: tuple[float, float] = (8.0, 25.0),
        static_duration_range: tuple[float, float] = (15.0, 50.0),
        osc_duration_range: tuple[float, float] = (25.0, 80.0),
        noise_w: float = 5.0,
        start_oscillating: bool = False,
    ) -> None:
        self._base = base_w
        self._osc_amp = osc_amplitude_w
        self._osc_period_range = osc_period_range
        self._static_dur = static_duration_range
        self._osc_dur = osc_duration_range
        self._noise_sigma = noise_w / 2.0

        self._state = _PhaseState.OSCILLATING if start_oscillating else _PhaseState.STATIC
        self._phase_start = time.monotonic()
        self._phase_duration = (
            random.uniform(*osc_duration_range)  # noqa: S311
            if start_oscillating
            else random.uniform(*static_duration_range)  # noqa: S311
        )
        self._static_level = base_w
        self._osc_period = random.uniform(*osc_period_range)  # noqa: S311

    def sample(self) -> float:
        now = time.monotonic()
        if now - self._phase_start >= self._phase_duration:
            self._next_phase(now)
        noise = random.gauss(0.0, self._noise_sigma)  # noqa: S311
        if self._state is _PhaseState.STATIC:
            return max(0.0, self._static_level + noise)
        t = now - self._phase_start
        osc = self._osc_amp * max(0.0, math.sin(2 * math.pi * t / self._osc_period))
        return max(0.0, self._base + osc + noise)

    def _next_phase(self, now: float) -> None:
        if self._state is _PhaseState.OSCILLATING:
            self._state = _PhaseState.STATIC
            self._phase_duration = random.uniform(*self._static_dur)  # noqa: S311
            spread = self._base * 0.3
            self._static_level = max(10.0, self._base + random.uniform(-spread, spread))  # noqa: S311
        else:
            self._state = _PhaseState.OSCILLATING
            self._phase_duration = random.uniform(*self._osc_dur)  # noqa: S311
            self._osc_period = random.uniform(*self._osc_period_range)  # noqa: S311
        self._phase_start = now


# ── Mock Grid Meter ───────────────────────────────────────────────────────────


class MockGridMeter:
    """Simulated Shelly 3EM.

    Phase A and C (feedforward):  OscillatingLoad – alternates between static
        and oscillating states at random intervals and frequencies.
    Phase B (feedback/battery):   Constant base load with noise.

    IMPORTANT – sign convention:
        The Shelly measures actual grid flow (positive = import from grid).
        When the battery feeds in, grid import decreases.  Therefore the battery
        AC output is SUBTRACTED from phase B's reading so that
        ``real_consumption_w = grid_total + battery_output_w`` stays roughly
        constant (as it should – real load doesn't change with battery output).
    """

    def __init__(
        self,
        load_a_w: float = 150.0,
        load_b_w: float = 250.0,
        load_c_w: float = 100.0,
        noise_w: float = 5.0,
        *,
        battery=None,  # SolarFlowAsyncMockClient – used to subtract AC output from phase B
    ) -> None:
        self._load_a = OscillatingLoad(
            load_a_w,
            osc_amplitude_w=80.0,
            osc_period_range=(8.0, 25.0),
            static_duration_range=(15.0, 45.0),
            osc_duration_range=(25.0, 80.0),
            noise_w=noise_w,
            start_oscillating=False,
        )
        self._load_c = OscillatingLoad(
            load_c_w,
            osc_amplitude_w=50.0,
            osc_period_range=(12.0, 40.0),
            static_duration_range=(20.0, 60.0),
            osc_duration_range=(30.0, 100.0),
            noise_w=noise_w,
            start_oscillating=True,  # C starts already oscillating → variety from t=0
        )
        self._base_b = load_b_w
        self._noise_sigma = noise_w / 2.0
        self._battery = battery

    async def get_phase_powers(self) -> Optional[tuple[float, float, float]]:
        # Battery output (positive = discharging) reduces grid import on phase B.
        batt_out = 0.0
        if self._battery is not None:
            raw = await self._battery.get_ac_output_power()
            batt_out = float(raw) if raw is not None else 0.0

        a = self._load_a.sample()
        c = self._load_c.sample()
        b = self._base_b + random.gauss(0.0, self._noise_sigma) - batt_out  # noqa: S311
        return (a, b, c)

    async def get_total_power(self) -> Optional[float]:
        phases = await self.get_phase_powers()
        return sum(phases) if phases else None


# ── Main ──────────────────────────────────────────────────────────────────────


async def run(
    host: str,
    port: int,
    load_a: float,
    load_b: float,
    load_c: float,
    noise: float,
    max_output: int,
    min_discharge: int,
    initial_mode: str,
) -> None:
    # ── Mock clients ──────────────────────────────────────────────────────────
    battery = SolarFlowAsyncMockClient()
    grid_meter = MockGridMeter(
        load_a_w=load_a,
        load_b_w=load_b,
        load_c_w=load_c,
        noise_w=noise,
        battery=battery,  # subtract battery output from phase B grid reading
    )
    LOG.info(
        "Mock hardware: battery=SolarFlowAsyncMockClient  "
        "Grid: A=%.0f W  B=%.0f W  C=%.0f W  noise=%.0f W  A+C=oscillating",
        load_a,
        load_b,
        load_c,
        noise,
    )

    # ── Runtime ───────────────────────────────────────────────────────────────
    runtime = ControlRuntime(
        grid_meter=grid_meter,
        battery=battery,
        sampling_interval_s=1.0,
        control_interval_s=3.0,
        max_discharge_w=max_output,
        min_discharge_w=min_discharge,
    )

    # ── Register regulators ───────────────────────────────────────────────────

    v4_yaml = _V4_CONFIG
    v4_settings = ZeroFeedV4Config(
        max_output_w=max_output,
        min_output_w=min_discharge,
    )
    v4 = ZeroFeedV4Regulator(settings=v4_settings, yaml_path=v4_yaml)
    runtime.register_regulator(v4)
    LOG.info("V4 settings: %s (file: %s)", v4_yaml, "present" if v4_yaml.exists() else "new")

    # ── Initial mode ──────────────────────────────────────────────────────────
    mode_map = {"idle": DeviceMode.IDLE, "zero_feed": DeviceMode.DISCHARGE_ZERO_FEED}
    await runtime.set_mode(mode_map.get(initial_mode, DeviceMode.IDLE))
    await runtime.start()
    LOG.info("ControlRuntime started (mode: %s)", initial_mode)

    # ── HTTP Server ───────────────────────────────────────────────────────────
    app = create_app(runtime)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    LOG.info("Dashboard available at http://%s:%d", host, port)

    try:
        await server.serve()
    except asyncio.CancelledError:
        pass
    finally:
        server.should_exit = True
        await runtime.stop()
        LOG.info("Dashboard stopped")


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Dashboard server with mock hardware (no Shelly/Zendure needed)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="127.0.0.1", metavar="HOST")
    parser.add_argument("--port", type=int, default=8765, metavar="PORT")
    parser.add_argument(
        "--load-a", type=float, default=150.0, metavar="W", help="Constant load on phase A [W]"
    )
    parser.add_argument(
        "--load-b", type=float, default=250.0, metavar="W", help="Constant load on phase B [W]"
    )
    parser.add_argument(
        "--load-c", type=float, default=100.0, metavar="W", help="Constant load on phase C [W]"
    )
    parser.add_argument("--noise", type=float, default=5.0, metavar="W", help="Random noise ± W")
    parser.add_argument("--max-output", type=int, default=800, metavar="W")
    parser.add_argument("--min-discharge", type=int, default=20, metavar="W")
    parser.add_argument(
        "--initial-mode",
        choices=["idle", "zero_feed"],
        default="idle",
        help="Initial mode",
    )
    parser.add_argument("--verbose", action="store_true")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    try:
        asyncio.run(
            run(
                host=args.host,
                port=args.port,
                load_a=args.load_a,
                load_b=args.load_b,
                load_c=args.load_c,
                noise=args.noise,
                max_output=args.max_output,
                min_discharge=args.min_discharge,
                initial_mode=args.initial_mode,
            )
        )
    except KeyboardInterrupt:
        LOG.info("Stopped by user")


if __name__ == "__main__":
    main()

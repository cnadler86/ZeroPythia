"""Unit tests for the feed-in watchdog in ControlRuntime.

Covers:
- Watchdog fires after sustained export exceeds trigger time
- Watchdog cooldown prevents rapid re-triggering
- Watchdog resets on normal grid draw
- Regulator reset and setpoint reduction on trigger
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional, cast

import pytest

from ZeroPythia.runtime.control_runtime import BypassResumeGuard, ControlRuntime
from ZeroPythia.runtime.models import DeviceMode, GridSample, ZFIState


# ── Fakes ──────────────────────────────────────────────────────────────────────


class FakeGrid:
    async def get_phase_powers(self):
        return (50.0, 30.0, 20.0)

    async def get_total_power(self):
        return 100.0


class FakeBattery:
    def __init__(self):
        self.commands: list[tuple[str, Optional[int]]] = []
        self.max_charge_power: Optional[int] = None
        self.max_discharge_power: Optional[int] = None

    async def get_ac_output_power(self):
        return 200

    async def get_state(self):
        return type("S", (), {"battery_soc": 50, "grid_input_power": 0})()

    async def get_min_soc(self, *, use_cache=True):
        return 15

    async def get_max_soc(self, *, use_cache=True):
        return 100

    async def start_discharge(self):
        self.commands.append(("discharge", None))
        return 20

    async def start_charge(self):
        self.commands.append(("charge", None))
        return 20

    async def set_ac_input_limit(self, power_w: int):
        self.commands.append(("input_limit", power_w))
        return power_w

    async def set_ac_output_limit(self, power_w: int):
        self.commands.append(("output_limit", power_w))
        return power_w

    async def stop(self):
        self.commands.append(("stop", None))
        return True

    async def get_ac_output_limit(self):
        return 800

    async def get_ac_input_limit(self):
        return 400

    async def is_settled(self, *, use_cache=True):
        return True


class FakeRegulator:
    name = "fake"
    description = "fake"

    def __init__(self):
        self.reset_calls = 0

    async def add_sample(self, sample):
        pass

    async def compute_setpoint(self, battery, max_w, min_w):
        pass

    def get_control_status(self):
        return None

    def settings_schema(self):
        return {}

    def get_current_settings(self):
        return {}

    def apply_settings(self, data):
        pass

    def reset(self):
        self.reset_calls += 1

    def bypass_resume_window_s(self):
        return None


def _make_sample(*, grid_w: float, timestamp: float | None = None) -> GridSample:
    split = grid_w / 3
    return GridSample(
        timestamp=timestamp or time.time(),
        phase_a_w=split,
        phase_b_w=split,
        phase_c_w=split,
        battery_output_w=0.0,
    )


def _make_runtime(
    battery: FakeBattery,
    *,
    control_interval_s: float = 3.0,
    watchdog_cycles: int = 3,
    watchdog_threshold_w: float = -10.0,
) -> ControlRuntime:
    rt = ControlRuntime(
        FakeGrid(),
        cast(Any, battery),
        control_interval_s=control_interval_s,
        watchdog_threshold_w=watchdog_threshold_w,
    )
    rt._min_soc_pct = 15
    rt._full_soc_pct = 100
    rt._min_soc_resume_pct = 20
    rt._watchdog_trigger_s = control_interval_s * watchdog_cycles + 1.0
    rt._watchdog_cooldown_s = 2.0 * rt._watchdog_trigger_s
    return rt


# ── Tests ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_watchdog_fires_after_sustained_export() -> None:
    """Sustained feed-in beyond trigger time must reset regulator and reduce setpoint."""
    batt = FakeBattery()
    rt = _make_runtime(batt, control_interval_s=1.0, watchdog_cycles=2)
    reg = FakeRegulator()
    rt._active_regulator = cast(Any, reg)
    rt._mode = DeviceMode.DISCHARGE_ZERO_FEED
    rt._zfi_state = ZFIState.RUNNING

    # trigger_time = 1.0 * 2 + 1.0 = 3.0 s
    mono_start = 0.0
    # Feed-in samples for > 3 seconds
    for i in range(5):
        sample = _make_sample(grid_w=-20.0)
        await rt._check_feed_in_watchdog(sample, mono_start + i)

    assert reg.reset_calls >= 1
    assert ("output_limit", rt._min_discharge_w) in batt.commands


@pytest.mark.asyncio
async def test_watchdog_resets_on_normal_draw() -> None:
    """Normal grid draw (positive) should reset the watchdog timer."""
    batt = FakeBattery()
    rt = _make_runtime(batt, control_interval_s=1.0, watchdog_cycles=2)
    reg = FakeRegulator()
    rt._active_regulator = cast(Any, reg)
    rt._mode = DeviceMode.DISCHARGE_ZERO_FEED
    rt._zfi_state = ZFIState.RUNNING

    # 2 feed-in samples
    sample_neg = _make_sample(grid_w=-20.0)
    await rt._check_feed_in_watchdog(sample_neg, 0.0)
    await rt._check_feed_in_watchdog(sample_neg, 1.0)

    # Normal draw → resets timer
    sample_pos = _make_sample(grid_w=50.0)
    await rt._check_feed_in_watchdog(sample_pos, 2.0)

    assert rt._watchdog_violation_since is None
    assert reg.reset_calls == 0


@pytest.mark.asyncio
async def test_watchdog_cooldown_prevents_rapid_retrigger() -> None:
    """After a watchdog fire, cooldown period must prevent immediate re-triggering."""
    batt = FakeBattery()
    rt = _make_runtime(batt, control_interval_s=1.0, watchdog_cycles=1)
    reg = FakeRegulator()
    rt._active_regulator = cast(Any, reg)
    rt._mode = DeviceMode.DISCHARGE_ZERO_FEED
    rt._zfi_state = ZFIState.RUNNING

    # trigger_time = 1.0 * 1 + 1.0 = 2.0 s; cooldown = 2 * 2.0 = 4.0 s
    # First trigger at t=2 (violation from t=0, duration=2.0 >= trigger_time)
    for i in range(4):
        await rt._check_feed_in_watchdog(_make_sample(grid_w=-20.0), float(i))

    first_resets = reg.reset_calls
    assert first_resets >= 1
    # _watchdog_last_reset ≈ 2.0, cooldown ends at 6.0

    # Continue with feed-in up to t=5 → still within cooldown (< 6.0)
    for i in range(4, 6):
        await rt._check_feed_in_watchdog(_make_sample(grid_w=-20.0), float(i))

    assert reg.reset_calls == first_resets  # no additional reset during cooldown

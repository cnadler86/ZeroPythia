"""Unit tests for SoC-based ZFI guards and AC charge limit in ControlRuntime.

Tests run without a real MQTT broker or hardware – pure in-process logic.

Covers:
* test_zfi_pauses_at_min_soc                          – low SoC → ZFI paused, battery stopped
* test_zfi_resumes_after_hysteresis                   – SoC rises above (min + hysteresis) → ZFI running
* test_zfi_no_premature_resume                        – SoC in hysteresis → soft-limit
* test_full_battery_pauses_zfi                        – SoC=100, low load → ZFI paused_full
* test_full_battery_resumes_immediately_at_max_soc    – SoC=100, high load → ZFI running
* test_full_battery_rest_cleared_when_not_in_zfi_mode – AC_CHARGE mode → ZFI inactive
* test_high_soc_charge_limit                          – SoC > 90% reduces AC charge to half
* test_zfi_plan_step_uses_config_max                  – AutoModeManager dispatches config_max_w for ZFI
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional, cast

import pytest

from src.runtime.control_runtime import ControlRuntime
from src.runtime.models import DeviceMode, GridSample, ZFIState


# ── Minimal fakes ─────────────────────────────────────────────────────────────


class FakeGrid:
    def __init__(self, *, phase_a: float = 50.0, phase_b: float = 30.0, phase_c: float = 20.0):
        self.phase_a = phase_a
        self.phase_b = phase_b
        self.phase_c = phase_c

    async def get_phase_powers(self):
        return (self.phase_a, self.phase_b, self.phase_c)

    async def get_total_power(self) -> float:
        return self.phase_a + self.phase_b + self.phase_c


class FakeBattery:
    def __init__(self, *, soc: int = 50, min_soc: int = 15, max_soc: int = 100):
        self.soc = soc
        self._min_soc = min_soc
        self._max_soc = max_soc
        self.commands: list[tuple[str, Optional[int]]] = []

    async def get_ac_output_power(self) -> int:
        return 200

    async def get_state(self):
        return type("S", (), {"battery_soc": self.soc, "grid_input_power": 0})()

    async def get_min_soc(self, *, use_cache: bool = True) -> int:
        return self._min_soc

    async def get_max_soc(self, *, use_cache: bool = True) -> int:
        return self._max_soc

    async def start_discharge(self) -> int:
        self.commands.append(("discharge", None))
        return 20  # min_power

    async def start_charge(self) -> int:
        self.commands.append(("charge", None))
        return 20  # min_power

    async def set_ac_input_limit(self, power_w: int) -> int:
        self.commands.append(("input_limit", power_w))
        return power_w

    async def set_ac_output_limit(self, power_w: int) -> int:
        self.commands.append(("output_limit", power_w))
        return power_w

    async def stop(self) -> bool:
        self.commands.append(("stop", None))
        return True

    async def get_ac_output_limit(self) -> Optional[int]:
        return 800

    async def get_ac_input_limit(self) -> Optional[int]:
        return 400

    async def is_settled(self, *, use_cache: bool = True) -> Optional[bool]:
        return True


class FakeRegulator:
    """Minimal RegulatorBase-compatible fake that records calls."""

    name = "fake"
    description = "fake regulator for tests"

    def __init__(self):
        self.samples: list[GridSample] = []
        self.setpoint_calls: int = 0
        self.reset_calls: int = 0

    async def add_sample(self, sample: GridSample) -> None:
        self.samples.append(sample)

    async def compute_setpoint(self, battery, max_output_w: int, min_output_w: int) -> None:
        self.setpoint_calls += 1

    def get_control_status(self):
        return None

    def settings_schema(self) -> dict:
        return {}

    def get_current_settings(self) -> dict:
        return {}

    async def update_settings(self, settings: dict) -> None:
        pass

    def reset(self) -> None:
        self.reset_calls += 1


def _make_sample(
    *,
    soc: Optional[int],
    grid_w: float = 100.0,
    battery_output_w: float = 0.0,
    charge_input_w: Optional[float] = None,
) -> GridSample:
    total = grid_w
    split = total / 3
    return GridSample(
        timestamp=time.time(),
        phase_a_w=split,
        phase_b_w=split,
        phase_c_w=split,
        battery_output_w=battery_output_w,
        soc_percent=soc,
        charge_input_w=charge_input_w,
    )


def _make_runtime(
    battery: FakeBattery,
    *,
    min_soc_pct: int = 15,
    min_soc_hysteresis_pct: int = 5,
    full_soc_pct: int = 100,
    full_soc_resume_delay_s: float = 10.0,
    full_soc_resume_threshold_w: int = 50,
    high_soc_charge_limit_pct: int = 90,
    high_soc_charge_limit_w: Optional[int] = None,
) -> ControlRuntime:
    rt = ControlRuntime(
        FakeGrid(),
        cast(Any, battery),
        min_soc_hysteresis_pct=min_soc_hysteresis_pct,
        full_soc_resume_delay_s=full_soc_resume_delay_s,
        full_soc_resume_threshold_w=full_soc_resume_threshold_w,
        high_soc_charge_limit_pct=high_soc_charge_limit_pct,
        high_soc_charge_limit_w=high_soc_charge_limit_w,
    )
    # Inject hardware SoC limits directly (avoids async start() in unit tests)
    rt._min_soc_pct = min_soc_pct
    rt._full_soc_pct = full_soc_pct
    rt._min_soc_resume_pct = min_soc_pct + min_soc_hysteresis_pct
    return rt


# ── Tests: Low-SoC Hysterese ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zfi_pauses_at_min_soc() -> None:
    """When SoC drops to min, ZFI should be paused and battery stopped."""
    batt = FakeBattery(soc=15)
    rt = _make_runtime(batt, min_soc_pct=15)
    rt._mode = DeviceMode.DISCHARGE_ZERO_FEED

    sample = _make_sample(soc=15)
    await rt._update_soc_guards(sample, time.monotonic())

    assert rt._zfi_state == ZFIState.PAUSED_LOW_SOC
    assert ("stop", None) in batt.commands


@pytest.mark.asyncio
async def test_zfi_no_premature_resume() -> None:
    """SoC between min and min+hysteresis must enter soft-limit, not full resume."""
    batt = FakeBattery(soc=18)
    rt = _make_runtime(batt, min_soc_pct=15, min_soc_hysteresis_pct=5)
    rt._mode = DeviceMode.DISCHARGE_ZERO_FEED
    rt._zfi_state = ZFIState.PAUSED_LOW_SOC  # already in full pause

    sample = _make_sample(soc=18)
    await rt._update_soc_guards(sample, time.monotonic())

    # SoC 18 > min 15 → transition to soft-limit (NOT staying in full pause,
    # and NOT a full resume either – power is capped at min_discharge_w)
    assert rt._zfi_state == ZFIState.SOFT_LIMITED
    assert ("discharge", None) in batt.commands  # soft-limit restarts at min_power


@pytest.mark.asyncio
async def test_zfi_resumes_after_hysteresis() -> None:
    """SoC >= min + hysteresis must resume ZFI and reset the regulator."""
    batt = FakeBattery(soc=20)
    rt = _make_runtime(batt, min_soc_pct=15, min_soc_hysteresis_pct=5)
    reg = FakeRegulator()
    rt._mode = DeviceMode.DISCHARGE_ZERO_FEED
    rt._active_regulator = cast(Any, reg)
    rt._zfi_state = ZFIState.PAUSED_LOW_SOC  # was paused

    sample = _make_sample(soc=20)
    await rt._update_soc_guards(sample, time.monotonic())

    assert rt._zfi_state == ZFIState.RUNNING
    assert reg.reset_calls == 1
    assert ("discharge", None) in batt.commands


# ── Tests: Vollbatterie-Pause ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_battery_pauses_zfi_when_load_not_above_threshold() -> None:
    """At max SoC, ZFI pauses only when household load is not above threshold."""
    batt = FakeBattery(soc=100)
    rt = _make_runtime(batt, full_soc_pct=100, full_soc_resume_threshold_w=50)
    rt._mode = DeviceMode.DISCHARGE_ZERO_FEED

    sample = _make_sample(soc=100, grid_w=30.0)
    await rt._update_soc_guards(sample, time.monotonic())

    assert rt._zfi_state == ZFIState.PAUSED_FULL
    assert ("stop", None) in batt.commands


@pytest.mark.asyncio
async def test_full_battery_resumes_immediately_at_max_soc_when_load_high() -> None:
    """At max SoC, high positive household load should resume discharge immediately."""
    batt = FakeBattery(soc=100)
    rt = _make_runtime(
        batt,
        full_soc_pct=100,
        full_soc_resume_threshold_w=50,
    )
    reg = FakeRegulator()
    rt._mode = DeviceMode.DISCHARGE_ZERO_FEED
    rt._active_regulator = cast(Any, reg)
    rt._zfi_state = ZFIState.PAUSED_FULL

    sample = _make_sample(soc=100, grid_w=150.0)
    await rt._update_soc_guards(sample, time.monotonic())

    assert rt._zfi_state == ZFIState.RUNNING
    assert reg.reset_calls == 1
    assert ("discharge", None) in batt.commands


@pytest.mark.asyncio
async def test_full_battery_rest_cleared_when_not_in_zfi_mode() -> None:
    """Switching away from ZFI mode must clear the full-battery rest state."""
    batt = FakeBattery(soc=100)
    rt = _make_runtime(batt, full_soc_pct=100)
    rt._mode = DeviceMode.AC_CHARGE  # NOT ZFI
    rt._zfi_state = ZFIState.PAUSED_FULL

    sample = _make_sample(soc=100)
    await rt._update_soc_guards(sample, time.monotonic())

    assert rt._zfi_state == ZFIState.INACTIVE


# ── Test: Upper SoC AC charge limit ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_high_soc_charge_limit_halves_charge_power() -> None:
    """SoC > 90% must reduce AC charge to half the current charge power."""
    batt = FakeBattery(soc=95)
    rt = _make_runtime(batt, high_soc_charge_limit_pct=90, high_soc_charge_limit_w=None)
    rt._mode = DeviceMode.AC_CHARGE
    rt._charge_power_w = 800  # currently charging at 800W

    sample = _make_sample(soc=95)
    await rt._apply_high_soc_charge_limit(sample)

    assert rt._charge_power_w == 400  # halved
    assert ("input_limit", 400) in batt.commands


@pytest.mark.asyncio
async def test_high_soc_charge_limit_uses_explicit_value() -> None:
    """Explicit high_soc_charge_limit_w must be used instead of halving."""
    batt = FakeBattery(soc=95)
    rt = _make_runtime(batt, high_soc_charge_limit_pct=90, high_soc_charge_limit_w=200)
    rt._mode = DeviceMode.AC_CHARGE
    rt._charge_power_w = 800

    sample = _make_sample(soc=95)
    await rt._apply_high_soc_charge_limit(sample)

    assert rt._charge_power_w == 200
    assert ("input_limit", 200) in batt.commands


@pytest.mark.asyncio
async def test_high_soc_charge_limit_no_action_below_threshold() -> None:
    """SoC below threshold must NOT trigger charge reduction."""
    batt = FakeBattery(soc=85)
    rt = _make_runtime(batt, high_soc_charge_limit_pct=90, high_soc_charge_limit_w=None)
    rt._mode = DeviceMode.AC_CHARGE
    rt._charge_power_w = 800

    sample = _make_sample(soc=85)
    await rt._apply_high_soc_charge_limit(sample)

    assert rt._charge_power_w == 800  # unchanged
    assert not batt.commands  # no battery commands


@pytest.mark.asyncio
async def test_high_soc_charge_limit_no_repeated_halving_from_throttled_value() -> None:
    """Throttle target must be derived from requested AC charge, not repeatedly halved."""
    batt = FakeBattery(soc=97)
    rt = _make_runtime(batt, high_soc_charge_limit_pct=90, high_soc_charge_limit_w=None)
    rt._mode = DeviceMode.AC_CHARGE
    rt._ac_charge_requested_power_w = 600
    rt._charge_power_w = 300  # already throttled once from 600 -> 300

    sample = _make_sample(soc=97)
    await rt._apply_high_soc_charge_limit(sample)

    assert rt._charge_power_w == 300
    assert not batt.commands


# ── Test: ZFI plan step uses config_max_w ─────────────────────────────────────


@pytest.mark.asyncio
async def test_zfi_plan_step_uses_config_max_w() -> None:
    """AutoModeManager must dispatch config_max_w (not discharge_ac_wh) for ZFI steps."""
    from datetime import timedelta

    from src.runtime.auto_mode import AutoModeManager

    dispatched: list[tuple] = []

    async def fake_cb(mode: DeviceMode, charge_w, max_dis_w) -> None:
        dispatched.append((mode, charge_w, max_dis_w))

    batt = FakeBattery(soc=50)
    config_max_w = 800

    mgr = AutoModeManager(
        mqtt_broker="mqtt://localhost:1883",
        device_id="TestDevice",
        battery=batt,
        config_max_w=config_max_w,
        config_min_w=20,
    )

    # Build a plan with a ZFI step that has a different discharge_ac_wh
    from src.gridpythia.models import InverterMode, InverterPlan, PlanStep  # noqa: PLC0415

    now = datetime.now(tz=timezone.utc)
    step = PlanStep(
        timestamp=now,
        mode=InverterMode.DISCHARGE_ZERO_FEED_IN,
        charge_ac_wh=0.0,
        discharge_ac_wh=50.0,  # would be 200W at dt=0.25h – irrelevant
        pv_to_ac_wh=0.0,
        pv_to_battery_wh=0.0,
        battery_soc_wh=None,
    )
    plan = InverterPlan(
        device_id="TestDevice",
        published_at=now,
        dt_hours=0.25,
        steps=[step],
    )
    # Inject plan directly
    cast(Any, mgr._subscriber)._plan = plan  # noqa: SLF001
    cast(Any, mgr._subscriber)._last_received_at = now  # noqa: SLF001

    await mgr.tick(fake_cb)

    assert len(dispatched) == 1
    mode, charge_w, max_dis_w = dispatched[0]
    assert mode == DeviceMode.DISCHARGE_ZERO_FEED
    assert max_dis_w == config_max_w, (
        f"Expected config_max_w={config_max_w}, got {max_dis_w}. "
        "ZFI step must not use discharge_ac_wh."
    )

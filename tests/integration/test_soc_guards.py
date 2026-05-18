"""Unit tests for SoC-based ZFI guards and AC charge limit in ControlRuntime.

Tests run without a real MQTT broker or hardware – pure in-process logic.

Covers:
* test_zfi_pauses_at_min_soc                          – low SoC → ZFI paused, battery stopped
* test_zfi_resumes_after_hysteresis                   – SoC rises above (min + hysteresis) → ZFI running
* test_zfi_no_premature_resume                        – SoC in hysteresis → soft-limit
* test_full_battery_pauses_zfi                        – SoC=100, low load → ZFI paused_full
* test_full_battery_resumes_after_guard_window        – guard window filled with high load → ZFI running
* test_full_battery_rest_cleared_when_not_in_zfi_mode – AC_CHARGE mode → ZFI inactive
* test_high_soc_charge_limit                          – SoC > 90% reduces AC charge to half
* test_zfi_plan_step_uses_config_max                  – AutoModeManager dispatches config_max_w for ZFI
* test_bypass_guard_blocks_resume_during_high_solar   – solar high, setpoints below required → stays paused
* test_bypass_guard_allows_resume_when_solar_zero     – no solar, high load → guard clears quickly
* test_bypass_guard_blocked_by_solar_spike            – a solar spike raises the required bar
* test_bypass_guard_reset_on_soc_drop                 – SoC < max → bypass guard reset, immediate resume
* test_bypass_resume_window_s_from_holder_config      – window derived from holder max_period * min_rising
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional, cast

import pytest

from ZeroPythia.runtime.control_runtime import BypassResumeGuard, ControlRuntime
from ZeroPythia.runtime.models import DeviceMode, GridSample, ZFIState


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
        self.max_charge_power: Optional[int] = None
        self.max_discharge_power: Optional[int] = None

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
    solar_input_w: Optional[float] = None,
    timestamp: Optional[float] = None,
) -> GridSample:
    total = grid_w
    split = total / 3
    return GridSample(
        timestamp=timestamp if timestamp is not None else time.time(),
        phase_a_w=split,
        phase_b_w=split,
        phase_c_w=split,
        battery_output_w=battery_output_w,
        soc_percent=soc,
        charge_input_w=charge_input_w,
        solar_input_w=solar_input_w,
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
    bypass_resume_safety_offset_w: float = 30.0,
    bypass_resume_window_s: float = 5.0,  # short window for tests
) -> ControlRuntime:
    rt = ControlRuntime(
        FakeGrid(),
        cast(Any, battery),
        min_soc_hysteresis_pct=min_soc_hysteresis_pct,
        full_soc_resume_delay_s=full_soc_resume_delay_s,
        full_soc_resume_threshold_w=full_soc_resume_threshold_w,
        high_soc_charge_limit_pct=high_soc_charge_limit_pct,
        high_soc_charge_limit_w=high_soc_charge_limit_w,
        bypass_resume_safety_offset_w=bypass_resume_safety_offset_w,
    )
    # Inject hardware SoC limits directly (avoids async start() in unit tests)
    rt._min_soc_pct = min_soc_pct
    rt._full_soc_pct = full_soc_pct
    rt._min_soc_resume_pct = min_soc_pct + min_soc_hysteresis_pct
    # Use a short window for tests so we don't need wall-clock delays
    rt._bypass_guard = BypassResumeGuard(
        window_s=bypass_resume_window_s,
        safety_offset_w=bypass_resume_safety_offset_w,
    )
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
async def test_full_battery_resumes_after_guard_window_with_high_load() -> None:
    """After the bypass guard window fills with consistently high load (no solar),
    ZFI should resume even though SoC is still at max."""
    batt = FakeBattery(soc=100)
    # window_s=5 → need 5 s of consistent samples; safety_offset=30 → load must be > 30 W
    rt = _make_runtime(
        batt,
        full_soc_pct=100,
        full_soc_resume_threshold_w=50,
        bypass_resume_safety_offset_w=30.0,
        bypass_resume_window_s=5.0,
    )
    reg = FakeRegulator()
    rt._mode = DeviceMode.DISCHARGE_ZERO_FEED
    rt._active_regulator = cast(Any, reg)
    rt._zfi_state = ZFIState.PAUSED_FULL

    # Send 6 samples spanning 6 s, each with high load and no solar.
    # theoretical_sp = grid(150) + solar(0) = 150 W  > solar_max(0) + offset(30) = 30 W → passes.
    base_ts = 1000.0
    for i in range(6):
        sample = _make_sample(
            soc=100, grid_w=150.0, solar_input_w=0.0, timestamp=base_ts + i
        )
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

    from ZeroPythia.runtime.auto_mode import AutoModeManager

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
    from ZeroPythia.gridpythia_bridge.models import InverterMode, InverterPlan, PlanStep  # noqa: PLC0415

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


# ── Tests: BypassResumeGuard unit tests ──────────────────────────────────────


def test_bypass_guard_not_ready_before_window_full() -> None:
    """Guard must not clear before the observation window is fully populated."""
    guard = BypassResumeGuard(window_s=10.0, safety_offset_w=30.0)
    base = 1000.0
    # Add samples for only 5 s (window = 10 s → not full)
    for i in range(6):
        guard.add_sample(base + i, theoretical_setpoint_w=300.0, solar_w=50.0)
    assert not guard.is_safe_to_start(base + 5)


def test_bypass_guard_clears_after_full_window_no_solar() -> None:
    """Guard must clear once the window is full and all setpoints exceed offset."""
    guard = BypassResumeGuard(window_s=5.0, safety_offset_w=30.0)
    base = 1000.0
    # solar = 0 → required = 0 + 30 = 30 W; theoretical_sp = 150 W → all pass
    for i in range(6):
        guard.add_sample(base + i, theoretical_setpoint_w=150.0, solar_w=0.0)
    assert guard.is_safe_to_start(base + 5)


def test_bypass_guard_blocks_when_setpoint_below_required() -> None:
    """Guard must stay blocked if any sample fails the solar+offset condition."""
    guard = BypassResumeGuard(window_s=5.0, safety_offset_w=30.0)
    base = 1000.0
    # solar_max = 200 W → required = 230 W; theoretical_sp = 220 W → fails
    for i in range(6):
        guard.add_sample(base + i, theoretical_setpoint_w=220.0, solar_w=200.0)
    assert not guard.is_safe_to_start(base + 5)


def test_bypass_guard_reset_clears_buffer() -> None:
    """After reset() the guard must not be ready regardless of previous samples."""
    guard = BypassResumeGuard(window_s=5.0, safety_offset_w=30.0)
    base = 1000.0
    for i in range(6):
        guard.add_sample(base + i, theoretical_setpoint_w=300.0, solar_w=0.0)
    assert guard.is_safe_to_start(base + 5)
    guard.reset()
    assert not guard.is_safe_to_start(base + 5)


def test_bypass_guard_solar_spike_raises_required_bar() -> None:
    """A high solar sample in the window raises solar_max, potentially blocking resume."""
    guard = BypassResumeGuard(window_s=5.0, safety_offset_w=30.0)
    base = 1000.0
    # First 5 samples: solar=50, theoretical_sp=200 (200 > 50+30=80 ✓)
    for i in range(5):
        guard.add_sample(base + i, theoretical_setpoint_w=200.0, solar_w=50.0)
    # 6th sample: solar spike to 250 W → solar_max=250 → required=280 W; sp=200 < 280 → fails
    guard.add_sample(base + 5, theoretical_setpoint_w=200.0, solar_w=250.0)
    assert not guard.is_safe_to_start(base + 5)


# ── Tests: Bypass guard integration with ControlRuntime ──────────────────────


@pytest.mark.asyncio
async def test_bypass_guard_blocks_resume_during_high_solar() -> None:
    """When solar is high and setpoints are insufficient, PAUSED_FULL must be maintained."""
    batt = FakeBattery(soc=100)
    # safety_offset=30, window=5s; solar=180W → required=210W; theoretical_sp=200W (grid=20+solar=180) → fails
    rt = _make_runtime(
        batt,
        full_soc_pct=100,
        bypass_resume_safety_offset_w=30.0,
        bypass_resume_window_s=5.0,
    )
    rt._mode = DeviceMode.DISCHARGE_ZERO_FEED
    rt._zfi_state = ZFIState.PAUSED_FULL

    base_ts = 1000.0
    for i in range(6):
        # grid=20W, solar=180W → theoretical_sp=200W < 210W required
        sample = _make_sample(
            soc=100, grid_w=20.0, solar_input_w=180.0, timestamp=base_ts + i
        )
        await rt._update_soc_guards(sample, time.monotonic())

    # Guard never cleared → still paused
    assert rt._zfi_state == ZFIState.PAUSED_FULL
    assert ("discharge", None) not in batt.commands


@pytest.mark.asyncio
async def test_bypass_guard_allows_resume_when_load_clearly_exceeds_solar() -> None:
    """With sufficient demand above solar + offset, guard clears and ZFI resumes."""
    batt = FakeBattery(soc=100)
    rt = _make_runtime(
        batt,
        full_soc_pct=100,
        bypass_resume_safety_offset_w=30.0,
        bypass_resume_window_s=5.0,
    )
    reg = FakeRegulator()
    rt._mode = DeviceMode.DISCHARGE_ZERO_FEED
    rt._active_regulator = cast(Any, reg)
    rt._zfi_state = ZFIState.PAUSED_FULL

    base_ts = 1000.0
    for i in range(6):
        # grid=100W, solar=150W → theoretical_sp=250W > 150+30=180W required → passes
        sample = _make_sample(
            soc=100, grid_w=100.0, solar_input_w=150.0, timestamp=base_ts + i
        )
        await rt._update_soc_guards(sample, time.monotonic())

    assert rt._zfi_state == ZFIState.RUNNING
    assert reg.reset_calls == 1
    assert ("discharge", None) in batt.commands


@pytest.mark.asyncio
async def test_bypass_guard_reset_on_soc_drop_below_max() -> None:
    """When SoC drops below max, bypass guard resets and ZFI resumes immediately."""
    batt = FakeBattery(soc=99)
    rt = _make_runtime(batt, full_soc_pct=100, bypass_resume_window_s=5.0)
    reg = FakeRegulator()
    rt._mode = DeviceMode.DISCHARGE_ZERO_FEED
    rt._active_regulator = cast(Any, reg)
    rt._zfi_state = ZFIState.PAUSED_FULL

    # Single sample at SoC=99 (below max=100) → bypass ended → immediate resume
    sample = _make_sample(soc=99, grid_w=100.0, solar_input_w=50.0, timestamp=1000.0)
    await rt._update_soc_guards(sample, time.monotonic())

    assert rt._zfi_state == ZFIState.RUNNING
    assert ("discharge", None) in batt.commands
    # Guard must have been reset
    assert len(rt._bypass_guard._buf) == 0


@pytest.mark.asyncio
async def test_bypass_guard_entry_resets_guard_on_fresh_pause() -> None:
    """Transitioning to PAUSED_FULL resets the guard so a clean window starts."""
    batt = FakeBattery(soc=100)
    rt = _make_runtime(
        batt,
        full_soc_pct=100,
        full_soc_resume_threshold_w=50,
        bypass_resume_safety_offset_w=30.0,
        bypass_resume_window_s=5.0,
    )
    rt._mode = DeviceMode.DISCHARGE_ZERO_FEED
    rt._zfi_state = ZFIState.RUNNING  # currently running

    # Pre-populate guard with old "good" samples
    for i in range(10):
        rt._bypass_guard.add_sample(900.0 + i, 300.0, 0.0)

    # Now send a low-load sample that triggers pause
    sample = _make_sample(soc=100, grid_w=20.0, solar_input_w=10.0, timestamp=1000.0)
    await rt._update_soc_guards(sample, time.monotonic())

    assert rt._zfi_state == ZFIState.PAUSED_FULL
    # Guard must have been reset – old stale samples are gone
    assert len(rt._bypass_guard._buf) == 0


# ── Test: bypass_resume_window_s() from ZeroFeedRegulator ────────────────────


def test_bypass_resume_window_s_from_holder_config() -> None:
    """Window must equal max_period * min_rising_count + 1 from all holder configs."""
    from ZeroPythia.config.zerofeed import ZeroFeedConfig
    from ZeroPythia.controller.oscillation_detectorv2 import BaseloadHolderSettings
    from ZeroPythia.controller.zerofeed_regulator import ZeroFeedRegulator

    cfg = ZeroFeedConfig()
    # Set holder for phase A: max_period=8, min_rising_count=3 → 8*3+1=25
    from ZeroPythia.config.zerofeed import FeedforwardPhaseConfig, OscillationConfig

    cfg.phases["A"] = FeedforwardPhaseConfig(
        osc=OscillationConfig(
            holder=BaseloadHolderSettings(max_period=8.0, min_rising_count=3),
        )
    )
    # Set holder for phase C: max_period=10, min_rising_count=2 → 10*2+1=21 (not max)
    cfg.phases["C"] = FeedforwardPhaseConfig(
        osc=OscillationConfig(
            holder=BaseloadHolderSettings(max_period=10.0, min_rising_count=2),
        )
    )
    reg = ZeroFeedRegulator(settings=cfg)
    # max_period=10, max_min_rising_count=3 → 10*3+1=31
    assert reg.bypass_resume_window_s() == pytest.approx(31.0)


def test_bypass_resume_window_s_fallback_when_no_holders() -> None:
    """Without any holder configs the window defaults to 25 s."""
    from ZeroPythia.config.zerofeed import ZeroFeedConfig
    from ZeroPythia.controller.zerofeed_regulator import ZeroFeedRegulator

    cfg = ZeroFeedConfig()
    # Disable all holders
    for ph_cfg in cfg.phases.values():
        ph_cfg.osc.holder = None
    reg = ZeroFeedRegulator(settings=cfg)
    assert reg.bypass_resume_window_s() == pytest.approx(25.0)

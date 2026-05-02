"""Tests for the bypass simulation in SolarFlowAsyncMockClient.

Verifies:
* Bypass activates when integer SOC reaches 100 %
* Bypass clears when integer SOC drops below 100 %
* output_home_power reflects solar power (not battery output) during bypass
* pack_input_power is 0 during bypass
* Setting outputLimit > solar forces bypass off
* start_discharge() bypass-kick forces bypass off and returns solar + overhead
* No SOC drain while bypass is active (battery not delivering)
"""
from __future__ import annotations

from time import time

import pytest

from clients.zendure.mock.async_mock_client import SolarFlowAsyncMockClient
from clients.zendure.models import ACMode


class FastMock(SolarFlowAsyncMockClient):
    """Instantaneous settling helpers so tests run without real delays."""

    async def _await_power_settled(self, *args, **kwargs) -> bool:  # noqa: ANN002
        return True

    async def _await_setpoint_confirmed(self, *args, **kwargs) -> bool:  # noqa: ANN002
        return True


# ---------------------------------------------------------------------------
# Bypass activation / deactivation
# ---------------------------------------------------------------------------


async def test_bypass_active_at_soc_100() -> None:
    """Mock must report bypass=True when initial SOC is 100."""
    client = FastMock(initial_soc=100)
    state = await client.get_state(use_cache=False)
    assert state is not None
    assert state.bypass_mode is True


async def test_bypass_inactive_below_soc_100() -> None:
    """Mock must report bypass=False when SOC is below 100."""
    client = FastMock(initial_soc=99)
    state = await client.get_state(use_cache=False)
    assert state is not None
    assert state.bypass_mode is False


async def test_bypass_activates_when_soc_hits_100() -> None:
    """_update_bypass sets _bypass=True when int(soc) >= 100."""
    client = FastMock(initial_soc=99)
    assert client._bypass is False

    client._soc = 100.0
    client._update_bypass()

    assert client._bypass is True


async def test_bypass_clears_when_soc_drops_to_99() -> None:
    """_update_bypass clears _bypass when int(soc) < 100."""
    client = FastMock(initial_soc=100)
    assert client._bypass is True

    client._soc = 99.9  # int(99.9) == 99 → bypass off
    client._update_bypass()

    assert client._bypass is False


# ---------------------------------------------------------------------------
# API response during bypass
# ---------------------------------------------------------------------------


async def test_output_home_power_reflects_solar_during_bypass() -> None:
    """When bypass is active, output_home_power must equal solar_input_power."""
    client = FastMock(initial_soc=100)
    client._solar_input_power = 250
    client._bypass = True

    output = await client.get_ac_output_power(use_cache=False)
    state = await client.get_state(use_cache=False)

    assert output == 250
    assert state is not None
    assert state.bypass_mode is True
    assert state.solar_input_power == 250


async def test_pack_input_power_zero_during_bypass() -> None:
    """pack_input_power (battery discharge current) must be 0 when bypass is active."""
    client = FastMock(initial_soc=100)
    client._solar_input_power = 200
    client._bypass = True
    # Force actual_output_power to a non-zero value to confirm it is hidden
    client._actual_output_power = 100

    response = await client._fetch_response()
    assert response is not None
    assert response.properties.pack_input_power == 0


# ---------------------------------------------------------------------------
# Forced bypass off
# ---------------------------------------------------------------------------


async def test_output_limit_above_solar_forces_bypass_off() -> None:
    """Setting outputLimit > solar must force bypass off in _set_new_setpoint."""
    client = FastMock(initial_soc=100)
    client._solar_input_power = 200
    client._bypass = True

    # 230 W > 200 W solar → bypass must be forced off
    await client.set_ac_output_limit(230)

    assert client._bypass is False


async def test_output_limit_equal_to_solar_leaves_bypass_on() -> None:
    """outputLimit == solar must NOT force bypass off (no surplus to draw from battery)."""
    client = FastMock(initial_soc=100)
    client._solar_input_power = 200
    client._bypass = True

    await client.set_ac_output_limit(200)

    # 200 W == 200 W solar, not strictly greater → bypass stays on
    assert client._bypass is True


# ---------------------------------------------------------------------------
# start_discharge bypass kick
# ---------------------------------------------------------------------------


async def test_start_discharge_bypass_kick_target_and_result() -> None:
    """start_discharge detects bypass → sets solar+overhead=230 W, bypass off."""
    client = FastMock(initial_soc=100)
    client._solar_input_power = 200
    client._bypass = True

    setpoint = await client.start_discharge()

    assert setpoint == 230  # 200 + bypass_overhead_w(30)
    assert client._bypass is False
    assert client._setpoint_w == 230
    assert client._current_mode == ACMode.OUTPUT


async def test_start_discharge_kick_clamped_to_discharge_limit() -> None:
    """Bypass kick target is clamped to the model discharge limit (800 W)."""
    client = FastMock(initial_soc=100)
    client._solar_input_power = 790  # 790 + 30 = 820 > 800
    client._bypass = True

    setpoint = await client.start_discharge()

    assert setpoint == 800  # clamped to discharge_limit


async def test_start_discharge_no_bypass_does_cold_start() -> None:
    """When bypass is off, start_discharge uses min_power cold start."""
    client = FastMock(initial_soc=80)
    assert client._bypass is False

    setpoint = await client.start_discharge()

    assert setpoint == 20  # min_power
    assert client._current_mode == ACMode.OUTPUT


# ---------------------------------------------------------------------------
# SOC drain during bypass
# ---------------------------------------------------------------------------


async def test_no_soc_drain_during_bypass() -> None:
    """Battery SOC must not decrease from AC output when bypass is active."""
    client = FastMock(initial_soc=100, battery_capacity_wh=1920)
    client._solar_input_power = 200
    client._bypass = True
    client._actual_output_power = 200  # would drain if bypass were off

    t0 = time()
    client.set_simulation_time(t0)
    client._last_update = t0

    # Advance 1 hour
    client.set_simulation_time(t0 + 3600.0)
    client._update_soc()

    # Solar adds energy but SOC is already at 100 (clamped)
    assert client._soc == pytest.approx(100.0)


async def test_soc_drains_after_bypass_forced_off() -> None:
    """After bypass is forced off, battery output must drain the SOC."""
    client = FastMock(initial_soc=100, battery_capacity_wh=1920)
    # Use instantaneous timing so setpoint power is available from t=0
    client.setpoint_delay = 0.0
    client.to_active_delay = 0.0
    client.pt1_time_constant = 0.0
    client.reaction_delay = 0.0
    client._solar_input_power = 0

    t0 = time()
    client.set_simulation_time(t0)
    client._last_update = t0
    client._soc = 100.0
    client._bypass = False  # bypass forced off; battery must deliver

    # Set a real discharge setpoint (200 W); _calculate_actual_power will return 200 W
    client._set_new_setpoint(ACMode.OUTPUT, 0, 200)

    # Advance 1 hour in simulation time
    client.set_simulation_time(t0 + 3600.0)
    client._update_soc()

    # Energy drained: 200 W / efficiency(0.92) * 1 h ≈ 217.4 Wh → ≈11.3 % of 1920 Wh
    drained_pct = 200 / client.EFFICIENCY / 1920 * 100
    assert client._soc == pytest.approx(100.0 - drained_pct, rel=0.05)

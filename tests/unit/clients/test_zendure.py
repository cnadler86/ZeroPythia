"""Unit tests for SolarFlowBase (via SolarFlowAsyncMockClient).

Covers:
* start_discharge() — cold start, already-discharging, and bypass-kick paths
* start_charge()    — cold start and already-charging paths
* set_ac_output_limit / set_ac_input_limit / stop — setpoint tracking
* _setpoint_w is always >= 0
* _flush_energy_to_now uses _current_mode, not sign
"""
from __future__ import annotations

import pytest

from clients.zendure.mock.async_mock_client import SolarFlowAsyncMockClient
from clients.zendure.models import ACMode


class FastMock(SolarFlowAsyncMockClient):
    """Mock with instantaneous settling helpers so tests run without real delays."""

    async def _await_power_settled(self, *args, **kwargs) -> bool:  # noqa: ANN002
        return True

    async def _await_setpoint_confirmed(self, *args, **kwargs) -> bool:  # noqa: ANN002
        return True


# ---------------------------------------------------------------------------
# start_discharge
# ---------------------------------------------------------------------------


async def test_start_discharge_cold_returns_min_power() -> None:
    """Cold start (SOC mid-range, no previous discharge) → returns min_power (20 W)."""
    client = FastMock(initial_soc=50)
    result = await client.start_discharge()

    assert result == 20  # min_power for solarFlow800Pro
    assert client._current_mode == ACMode.OUTPUT
    assert client._setpoint_w == 20
    assert client._setpoint_w >= 0


async def test_start_discharge_already_discharging_returns_current_setpoint() -> None:
    """Re-entering discharge when already active → re-sends existing setpoint, no two-step."""
    client = FastMock(initial_soc=60)
    client._current_mode = ACMode.OUTPUT
    client._setpoint_w = 200

    result = await client.start_discharge()

    assert result == 200
    assert client._setpoint_w == 200
    assert client._current_mode == ACMode.OUTPUT


async def test_start_discharge_bypass_kick_returns_solar_plus_overhead() -> None:
    """Bypass active → target = solar + bypass_overhead_w, bypass forced off."""
    client = FastMock(initial_soc=100)  # initial_soc=100 → _bypass=True
    client._solar_input_power = 200
    client._bypass = True  # ensure bypass is set

    result = await client.start_discharge()

    # target = solar(200) + overhead(30) = 230 W
    assert result == 230
    assert client._bypass is False
    assert client._setpoint_w == 230
    assert client._current_mode == ACMode.OUTPUT


async def test_start_discharge_bypass_kick_clamped_to_discharge_limit() -> None:
    """Bypass kick target is clamped to the device discharge limit (800 W)."""
    client = FastMock(initial_soc=100)
    client._solar_input_power = 800  # solar ≥ discharge_limit
    client._bypass = True

    result = await client.start_discharge()

    # min(max(20, 800+30=830), 800) = 800
    assert result == 800
    assert client._setpoint_w == 800


async def test_start_discharge_hardware_error_returns_zero() -> None:
    """When _set_properties fails, start_discharge must return 0."""
    client = FastMock(initial_soc=50)

    async def _fail(*args, **kwargs):  # noqa: ANN002
        return False

    client._set_properties = _fail  # type: ignore[method-assign]

    result = await client.start_discharge()

    assert result == 0
    # Setpoint must NOT have been updated on failure
    assert client._setpoint_w == 0


async def test_start_discharge_setpoint_w_always_non_negative() -> None:
    """_setpoint_w must never go negative."""
    client = FastMock(initial_soc=50)
    await client.start_discharge()
    assert client._setpoint_w >= 0


# ---------------------------------------------------------------------------
# start_charge
# ---------------------------------------------------------------------------


async def test_start_charge_cold_returns_min_power() -> None:
    """Cold start → returns min_power (20 W) and sets INPUT mode."""
    client = FastMock(initial_soc=50)
    result = await client.start_charge()

    assert result == 20
    assert client._current_mode == ACMode.INPUT
    assert client._setpoint_w == 20
    assert client._setpoint_w >= 0


async def test_start_charge_already_charging_returns_current_setpoint() -> None:
    """Re-entering charge when already active → re-sends existing setpoint."""
    client = FastMock(initial_soc=40)
    client._current_mode = ACMode.INPUT
    client._setpoint_w = 400

    result = await client.start_charge()

    assert result == 400
    assert client._setpoint_w == 400
    assert client._current_mode == ACMode.INPUT


async def test_start_charge_hardware_error_returns_zero() -> None:
    """When _set_properties fails, start_charge must return 0."""
    client = FastMock(initial_soc=50)

    async def _fail(*args, **kwargs):  # noqa: ANN002
        return False

    client._set_properties = _fail  # type: ignore[method-assign]

    result = await client.start_charge()

    assert result == 0
    assert client._setpoint_w == 0


async def test_start_charge_mode_switch_from_discharge() -> None:
    """Direct discharge→charge transition sets correct mode."""
    client = FastMock(initial_soc=50)
    client._current_mode = ACMode.OUTPUT
    client._setpoint_w = 200

    result = await client.start_charge()

    assert result == 20
    assert client._current_mode == ACMode.INPUT


# ---------------------------------------------------------------------------
# set_ac_output_limit / set_ac_input_limit / stop
# ---------------------------------------------------------------------------


async def test_set_ac_output_limit_updates_setpoint_on_success() -> None:
    client = FastMock(initial_soc=50)
    applied = await client.set_ac_output_limit(300)

    assert applied == 300
    assert client._setpoint_w == 300
    assert client._current_mode == ACMode.OUTPUT
    assert client._setpoint_w >= 0


async def test_set_ac_input_limit_updates_setpoint_and_mode() -> None:
    client = FastMock(initial_soc=50)
    applied = await client.set_ac_input_limit(500)

    assert applied == 500
    assert client._setpoint_w == 500
    assert client._current_mode == ACMode.INPUT
    assert client._setpoint_w >= 0


async def test_stop_clears_setpoint_and_mode() -> None:
    client = FastMock(initial_soc=50)
    client._current_mode = ACMode.INPUT
    client._setpoint_w = 400

    ok = await client.stop()

    assert ok is True
    assert client._setpoint_w == 0
    assert client._current_mode == ACMode.OUTPUT


async def test_setpoint_never_negative_through_charge_cycle() -> None:
    """Full charge→discharge→stop cycle: _setpoint_w stays >= 0 throughout."""
    client = FastMock(initial_soc=50)

    await client.set_ac_input_limit(400)
    assert client._setpoint_w >= 0

    await client.set_ac_output_limit(200)
    assert client._setpoint_w >= 0

    await client.stop()
    assert client._setpoint_w == 0


# ---------------------------------------------------------------------------
# Energy accounting via _current_mode
# ---------------------------------------------------------------------------


async def test_flush_energy_discharge_direction() -> None:
    """Energy flushed with OUTPUT mode must go to discharge_wh, not charge_wh."""
    client = FastMock(initial_soc=50)
    client._current_mode = ACMode.OUTPUT
    client._setpoint_w = 400
    client._setpoint_timestamp = client._get_time() - 3600.0  # 1 hour

    counters = client.get_energy_counters()

    assert counters.discharge_wh == pytest.approx(400.0, rel=0.01)
    assert counters.charge_wh == pytest.approx(0.0)


async def test_flush_energy_charge_direction() -> None:
    """Energy flushed with INPUT mode must go to charge_wh, not discharge_wh."""
    client = FastMock(initial_soc=50)
    client._current_mode = ACMode.INPUT
    client._setpoint_w = 300
    client._setpoint_timestamp = client._get_time() - 3600.0  # 1 hour

    counters = client.get_energy_counters()

    assert counters.charge_wh == pytest.approx(300.0, rel=0.01)
    assert counters.discharge_wh == pytest.approx(0.0)


async def test_flush_energy_zero_setpoint() -> None:
    """No energy accumulation when setpoint is 0."""
    client = FastMock(initial_soc=50)
    client._setpoint_w = 0
    client._setpoint_timestamp = client._get_time() - 3600.0

    counters = client.get_energy_counters()

    assert counters.discharge_wh == pytest.approx(0.0)
    assert counters.charge_wh == pytest.approx(0.0)

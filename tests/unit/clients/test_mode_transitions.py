"""Tests für atomare Moduswechsel zwischen Charge, Discharge und Idle.

Sichergestellt wird:
* Jeder Moduswechsel (Charge↔Discharge, *→Idle) findet in EINEM einzigen
  _set_properties-Aufruf statt – kein Zwischenschritt durch Idle, der die
  Schützen zusätzlich belastet.
* Beim Wechsel von Charge→Discharge gilt in DIESEM einen Aufruf:
    inputLimit == 0  UND  outputLimit > 0
* Beim Wechsel von Discharge→Charge gilt in DIESEM einen Aufruf:
    outputLimit == 0  UND  inputLimit > 0
* stop() setzt beide Limits atomar auf 0.
* start_discharge() wartet beim Kaltstart auf physisches Settling
  (_await_power_settled) UND auf API-Bestätigung des Setpoints.
* Beim Kaltstart mit Ziel > min_power (Bypass-Kick) wird Phase 1 ebenfalls
  atomar gesendet.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from clients.zendure.mock.async_mock_client import SolarFlowAsyncMockClient
from clients.zendure.models import ACMode


# ---------------------------------------------------------------------------
# Spy fixture
# ---------------------------------------------------------------------------


class SpyMock(SolarFlowAsyncMockClient):
    """Spy that records every _set_properties call.

    Uses zero-delay / zero-noise timing so _await_power_settled and
    _await_setpoint_confirmed return immediately without real time passing.
    The wait methods themselves are NOT bypassed – the actual base-class logic runs.
    This allows tests to verify that the correct waits are invoked, while the mock
    settles on the very first poll because:
      - to_active_delay   = 0  → no standby reaction delay
      - setpoint_delay    = 0  → setpoint visible in API immediately
      - pt1_time_constant = 0  → PT1 jumps to target in one step
      - OUTPUT_NOISE_W    = 0  ↗ no random noise that could fail
      - STEP_NOISE_FRACTION = 0 ↗ the ≤2 W check
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[dict] = []
        # Zero-delay, zero-noise: device appears settled on the first poll
        self.to_active_delay = 0.0
        self.setpoint_delay = 0.0
        self.pt1_time_constant = 0.0
        self.OUTPUT_NOISE_W = 0.0
        self.STEP_NOISE_FRACTION = 0.0

    async def _await_power_settled(
        self,
        target_w: int,
        *,
        is_charge: bool = False,
        timeout_s: float = 15.0,
        poll_s: float = 1.0,
    ) -> bool:
        # Fast poll: asyncio.sleep(0) so the check completes in a single tick
        return await super()._await_power_settled(
            target_w, is_charge=is_charge, timeout_s=timeout_s, poll_s=0.0
        )

    async def _await_setpoint_confirmed(
        self,
        expected_w: int,
        *,
        is_charge: bool = False,
        timeout_s: float = 5.0,
        poll_s: float = 0.5,
    ) -> bool:
        return await super()._await_setpoint_confirmed(
            expected_w, is_charge=is_charge, timeout_s=timeout_s, poll_s=0.0
        )

    async def _set_properties(self, properties: dict, smart_mode: bool = True) -> bool:
        # Record without the internal smartMode key
        self.calls.append({k: v for k, v in properties.items() if k != "smartMode"})
        return await super()._set_properties(properties, smart_mode)

    def reset_calls(self) -> None:
        self.calls.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_mode_change_call(calls: list[dict]) -> dict:
    """Return the first call that changes acMode or both limits simultaneously."""
    for c in calls:
        if "acMode" in c or ("inputLimit" in c and "outputLimit" in c):
            return c
    raise AssertionError(f"No mode-change call found in: {calls}")


# ---------------------------------------------------------------------------
# Charge → Discharge (atomar)
# ---------------------------------------------------------------------------


async def test_charge_to_discharge_single_atomic_rpc() -> None:
    """Transitioning from charge to discharge must happen in exactly ONE
    _set_properties call that clears inputLimit AND sets outputLimit in the
    same payload – no intermediate idle."""
    client = SpyMock(initial_soc=50)

    # Establish charge mode
    await client.start_charge()
    assert client._current_mode == ACMode.INPUT
    client.reset_calls()

    # Switch to discharge
    result = await client.start_discharge()

    assert result > 0, "start_discharge must return a positive setpoint"

    # The very FIRST call after the mode-switch must be atomic
    assert len(client.calls) >= 1, "Expected at least one _set_properties call"
    first = client.calls[0]

    assert "inputLimit" in first, f"First call must include inputLimit; got {first}"
    assert "outputLimit" in first, f"First call must include outputLimit; got {first}"
    assert first["inputLimit"] == 0, (
        f"inputLimit must be 0 in the transition call; got {first['inputLimit']}"
    )
    assert first["outputLimit"] > 0, (
        f"outputLimit must be >0 in the transition call; got {first['outputLimit']}"
    )


async def test_charge_to_discharge_acmode_output_in_first_call() -> None:
    """The first call must also set acMode to OUTPUT (not leave it implicit)."""
    client = SpyMock(initial_soc=50)
    await client.start_charge()
    client.reset_calls()

    await client.start_discharge()

    first = client.calls[0]
    assert "acMode" in first, f"acMode must be set explicitly in transition call; got {first}"
    assert first["acMode"] == ACMode.OUTPUT.value


async def test_charge_to_discharge_no_intermediate_idle() -> None:
    """There must be no call with outputLimit==0 AND inputLimit==0 between
    a charge→discharge transition (that would be an idle intermediate step)."""
    client = SpyMock(initial_soc=50)
    await client.start_charge()
    client.reset_calls()

    await client.start_discharge()

    idle_calls = [
        c for c in client.calls
        if c.get("outputLimit") == 0 and c.get("inputLimit") == 0
    ]
    assert idle_calls == [], (
        f"Found intermediate idle call(s) during charge→discharge: {idle_calls}"
    )


# ---------------------------------------------------------------------------
# Discharge → Charge (atomar)
# ---------------------------------------------------------------------------


async def test_discharge_to_charge_single_atomic_rpc() -> None:
    """Transitioning from discharge to charge must happen in exactly ONE
    _set_properties call that clears outputLimit AND sets inputLimit."""
    client = SpyMock(initial_soc=50)

    await client.start_discharge()
    assert client._current_mode == ACMode.OUTPUT
    client.reset_calls()

    result = await client.start_charge()

    assert result > 0, "start_charge must return a positive setpoint"
    assert len(client.calls) >= 1

    first = client.calls[0]
    assert "outputLimit" in first, f"First call must include outputLimit; got {first}"
    assert "inputLimit" in first, f"First call must include inputLimit; got {first}"
    assert first["outputLimit"] == 0, (
        f"outputLimit must be 0 in the transition call; got {first['outputLimit']}"
    )
    assert first["inputLimit"] > 0, (
        f"inputLimit must be >0 in the transition call; got {first['inputLimit']}"
    )


async def test_discharge_to_charge_acmode_input_in_first_call() -> None:
    """The first call must set acMode to INPUT."""
    client = SpyMock(initial_soc=50)
    await client.start_discharge()
    client.reset_calls()

    await client.start_charge()

    first = client.calls[0]
    assert "acMode" in first, f"acMode must be set explicitly; got {first}"
    assert first["acMode"] == ACMode.INPUT.value


async def test_discharge_to_charge_no_intermediate_idle() -> None:
    """No idle intermediate call during discharge→charge transition."""
    client = SpyMock(initial_soc=50)
    await client.start_discharge()
    client.reset_calls()

    await client.start_charge()

    idle_calls = [
        c for c in client.calls
        if c.get("outputLimit") == 0 and c.get("inputLimit") == 0
    ]
    assert idle_calls == [], (
        f"Found intermediate idle call(s) during discharge→charge: {idle_calls}"
    )


# ---------------------------------------------------------------------------
# stop() – atomar
# ---------------------------------------------------------------------------


async def test_stop_from_discharge_is_atomic() -> None:
    """stop() must send both limits to 0 in exactly one call (no two-step)."""
    client = SpyMock(initial_soc=50)
    await client.start_discharge()
    client.reset_calls()

    ok = await client.stop()

    assert ok is True
    assert len(client.calls) == 1, f"stop() must use exactly 1 RPC call; got {client.calls}"
    call = client.calls[0]
    assert call.get("outputLimit") == 0
    assert call.get("inputLimit") == 0


async def test_stop_from_charge_is_atomic() -> None:
    """stop() from charge mode must send both limits to 0 in one call."""
    client = SpyMock(initial_soc=50)
    await client.start_charge()
    client.reset_calls()

    ok = await client.stop()

    assert ok is True
    assert len(client.calls) == 1, f"stop() must use exactly 1 RPC call; got {client.calls}"
    call = client.calls[0]
    assert call.get("outputLimit") == 0
    assert call.get("inputLimit") == 0


# ---------------------------------------------------------------------------
# Discharge cold-start: initiales Warten auf Setpoint
# ---------------------------------------------------------------------------


async def test_start_discharge_cold_start_awaits_setpoint_confirmed() -> None:
    """Cold start at min_power calls _await_setpoint_confirmed before returning."""
    client = SpyMock(initial_soc=50)
    confirmed_called = False

    original = client._await_setpoint_confirmed

    async def tracking_confirmed(
        expected_w: int,
        *,
        is_charge: bool = False,
        timeout_s: float = 5.0,
        poll_s: float = 0.5,
    ) -> bool:
        nonlocal confirmed_called
        confirmed_called = True
        return await original(
            expected_w, is_charge=is_charge, timeout_s=timeout_s, poll_s=poll_s
        )

    client._await_setpoint_confirmed = tracking_confirmed  # type: ignore

    result = await client.start_discharge()

    assert result == 20  # min_power for solarFlow800Pro
    assert confirmed_called, (
        "start_discharge (cold start at min_power) must call _await_setpoint_confirmed"
    )


async def test_start_discharge_cold_start_awaits_power_settled() -> None:
    """Cold start at min_power must also wait for physical settling via
    _await_power_settled (10 s in real hardware, instantaneous in SpyMock).
    This prevents the regulator from ramping up before the inverter has
    physically reached the initial setpoint."""
    client = SpyMock(initial_soc=50)
    settled_called = False

    original = client._await_power_settled

    async def tracking_settled(
        target_w: int,
        *,
        is_charge: bool = False,
        timeout_s: float = 15.0,
        poll_s: float = 1.0,
    ) -> bool:
        nonlocal settled_called
        settled_called = True
        return await original(
            target_w, is_charge=is_charge, timeout_s=timeout_s, poll_s=poll_s
        )

    client._await_power_settled = tracking_settled  # type: ignore

    result = await client.start_discharge()

    assert result == 20
    assert settled_called, (
        "start_discharge (cold start) must call _await_power_settled before returning"
    )


async def test_start_discharge_bypass_kick_awaits_setpoint_confirmed() -> None:
    """Bypass-kick path uses a SINGLE RPC and waits for _await_setpoint_confirmed
    (not _await_power_settled – the latter is for the two-step cold-start branch
    which requires target > min_power, a condition currently unreachable without
    bypass since cold-start always sets target == min_power)."""
    client = SpyMock(initial_soc=100)
    client._solar_input_power = 200  # bypass active; target = 200 + 30 = 230 W
    client._bypass = True

    confirmed_called = False
    original = client._await_setpoint_confirmed

    async def tracking_confirmed(
        expected_w: int,
        *,
        is_charge: bool = False,
        timeout_s: float = 5.0,
        poll_s: float = 0.5,
    ) -> bool:
        nonlocal confirmed_called
        confirmed_called = True
        return await original(
            expected_w, is_charge=is_charge, timeout_s=timeout_s, poll_s=poll_s
        )

    client._await_setpoint_confirmed = tracking_confirmed  # type: ignore

    result = await client.start_discharge()

    assert result > 0
    assert confirmed_called, (
        "start_discharge (bypass-kick) must call _await_setpoint_confirmed"
    )
    # Only ONE _set_properties call – no two-step in bypass-kick path
    assert len(client.calls) == 1, (
        f"Bypass-kick must use exactly 1 RPC; got {client.calls}"
    )


async def test_start_discharge_bypass_kick_rpc_is_atomic() -> None:
    """Bypass-kick discharge start must send inputLimit=0 AND outputLimit>0 in one call."""
    client = SpyMock(initial_soc=100)
    client._solar_input_power = 200
    client._bypass = True

    await client.start_discharge()

    first = client.calls[0]
    assert "inputLimit" in first, f"Bypass-kick call must include inputLimit; got {first}"
    assert "outputLimit" in first, f"Bypass-kick call must include outputLimit; got {first}"
    assert first["inputLimit"] == 0
    assert first["outputLimit"] > 0


# ---------------------------------------------------------------------------
# Discharge → Idle → Discharge: korrekte Statusübergänge
# ---------------------------------------------------------------------------


async def test_idle_to_discharge_then_idle_state_consistent() -> None:
    """Full Discharge→Idle→Discharge cycle leaves _current_mode and _setpoint_w
    in consistent state at each step."""
    client = SpyMock(initial_soc=50)

    # Discharge
    await client.start_discharge()
    assert client._current_mode == ACMode.OUTPUT
    assert client._setpoint_w > 0

    # Idle
    await client.stop()
    assert client._current_mode == ACMode.OUTPUT
    assert client._setpoint_w == 0

    # Discharge again
    await client.start_discharge()
    assert client._current_mode == ACMode.OUTPUT
    assert client._setpoint_w > 0


async def test_charge_to_idle_to_discharge_no_idle_stutter() -> None:
    """Charge → stop → start_discharge must not introduce an extra idle RPC
    between stop and the new discharge command."""
    client = SpyMock(initial_soc=50)

    await client.start_charge()
    await client.stop()

    client.reset_calls()  # only watch the transition to discharge
    await client.start_discharge()

    idle_calls = [
        c for c in client.calls
        if c.get("outputLimit") == 0 and c.get("inputLimit") == 0
    ]
    assert idle_calls == [], (
        f"start_discharge after stop must not send an extra idle RPC: {idle_calls}"
    )

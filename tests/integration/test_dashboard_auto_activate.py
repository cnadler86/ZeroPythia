"""Test: Auto-mode activation from dashboard with correct defaults.

Reproduces the exact flow the user performs in the browser:
  1. Click 'Auto ▸' → auto-panel appears
  2. Fields are pre-filled (localhost:1883 / SF800Pro)
  3. Click 'Auto aktivieren' → POST /api/auto/connect
  4. Mode switches to AUTO, auto_status is populated
"""
from __future__ import annotations

import asyncio
import socket
from typing import Any, cast

import pytest

def _mqtt_reachable() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 1883), timeout=1.0):
            return True
    except OSError:
        return False

pytestmark = pytest.mark.skipif(
    not _mqtt_reachable(),
    reason="MQTT broker not available on localhost:1883",
)


class FakeGrid:
    async def get_phase_powers(self):
        return (50.0, 30.0, 20.0)

    async def get_total_power(self):
        return 100.0


class FakeBattery:
    _setpoint_w = 200

    async def get_ac_output_power(self):
        return 200

    async def start_charge(self):
        self._setpoint_w = 20
        return 20

    async def start_discharge(self):
        self._setpoint_w = 20
        return 20

    async def set_ac_input_limit(self, power_w: int):
        self._setpoint_w = power_w
        return True

    async def stop(self):
        return True

    async def get_ac_output_limit(self):
        return 800

    async def get_ac_input_limit(self):
        return 400

    async def is_settled(self, *, use_cache=True):
        return True

    async def get_state(self):
        return type("S", (), {"battery_soc": 75, "grid_input_power": 0})()

    async def get_battery_soc(self, *, use_cache=True):
        return 75


@pytest.mark.asyncio
async def test_html_defaults_are_correct():
    """HTML should ship with localhost:1883 and SF800Pro as pre-filled values."""
    from httpx import ASGITransport, AsyncClient

    from src.dashboard.regulators.v4_adapter import ZeroFeedV4Regulator
    from src.dashboard.runtime import ControlRuntime
    from src.dashboard.server import create_app

    runtime = ControlRuntime(FakeGrid(), cast(Any, FakeBattery()))
    runtime.register_regulator(ZeroFeedV4Regulator())
    app = create_app(runtime)

    async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as c:
        r = await c.get("/")
        html = r.text

    # Broker default
    assert 'value="mqtt://localhost:1883"' in html, "MQTT broker default should be localhost"
    # Device ID must be a value, not just a placeholder
    assert 'id="auto-device-id"' in html
    # value="SF800Pro" must appear (not just placeholder)
    assert 'value="SF800Pro"' in html, "Device ID must have value=SF800Pro, not just placeholder"


@pytest.mark.asyncio
async def test_auto_activate_from_dashboard():
    """Simulates the full click flow: open panel → fill defaults → activate auto mode."""
    import time

    from httpx import ASGITransport, AsyncClient

    from src.dashboard.models import DeviceMode
    from src.dashboard.regulators.v4_adapter import ZeroFeedV4Regulator
    from src.dashboard.runtime import ControlRuntime
    from src.dashboard.server import create_app

    battery = FakeBattery()
    runtime = ControlRuntime(
        FakeGrid(),
        cast(Any, battery),
        sampling_interval_s=0.1,
        control_interval_s=0.5,
    )
    runtime.register_regulator(ZeroFeedV4Regulator())
    await runtime.start()
    app = create_app(runtime)

    try:
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as c:
            # Step 1: user clicks 'Auto aktivieren' with the pre-filled form values
            r = await c.post(
                "/api/auto/connect",
                json={
                    "mqtt_broker": "mqtt://localhost:1883",
                    "device_id": "SF800Pro",
                    "topic_prefix": "gridpythia",
                    "status_interval_s": 60.0,
                },
            )
            assert r.status_code == 200, f"connect failed: {r.text}"
            assert r.json()["status"] == "ok"

            # Step 2: state must immediately reflect AUTO mode
            r2 = await c.get("/api/state")
            state = r2.json()
            assert state["mode"] == "auto", f"Expected mode=auto, got {state['mode']}"
            assert state["auto_status"] is not None
            assert state["auto_status"]["connected"] is True

            # Step 3: wait for the first tick → fallback (no plan) → DISCHARGE_ZERO_FEED effective
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                await asyncio.sleep(0.2)
                if runtime._auto_effective_mode == DeviceMode.DISCHARGE_ZERO_FEED:
                    break

            assert runtime._auto_effective_mode == DeviceMode.DISCHARGE_ZERO_FEED, (
                f"Effective mode after 5s: {runtime._auto_effective_mode}"
            )

            # Step 4: check auto_status in WS state
            r3 = await c.get("/api/state")
            state3 = r3.json()
            ast = state3["auto_status"]
            assert ast["connected"] is True
            # effective_mode may be fallback label
            assert "Zero-Feed" in ast["effective_mode"], f"Unexpected effective: {ast['effective_mode']}"

            # Step 5: deactivate
            r4 = await c.post("/api/auto/disconnect")
            assert r4.status_code == 200
            r5 = await c.get("/api/state")
            assert r5.json()["mode"] == "idle"
    finally:
        await runtime.stop()

"""End-to-end integration test: AutoModeManager ↔ localhost MQTT broker.

Verifies that:
1. A GridPythia-style plan published to localhost:1883 is received and parsed.
2. The AutoModeManager dispatches DISCHARGE_ZERO_FEED for a ZFI plan step.
3. The ControlRuntime routes the effective mode correctly.
4. The status reporter publishes a well-formed {soc, mode} message.

Requires a running MQTT broker on localhost:1883 (e.g. Mosquitto).
Skip automatically when the port is closed.
"""
from __future__ import annotations

import asyncio
import json
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, cast

import pytest

# ── Skip guard ────────────────────────────────────────────────────────────────


def _mqtt_reachable() -> bool:
    """Return True if localhost:1883 accepts TCP connections."""
    try:
        with socket.create_connection(("127.0.0.1", 1883), timeout=1.0):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _mqtt_reachable(),
    reason="MQTT broker not available on localhost:1883",
)


# ── Helpers / Fakes ───────────────────────────────────────────────────────────

DEVICE_ID = "TestInverter01"
TOPIC_PREFIX = "gridpythia_test"
PLAN_TOPIC = f"{TOPIC_PREFIX}/inverters/{DEVICE_ID}/plan"
STATUS_TOPIC = f"{TOPIC_PREFIX}/inverters/{DEVICE_ID}/status"


class FakeBattery:
    """Minimal BatteryInverterProtocol + SolarFlowBase-compatible fake."""

    def __init__(self) -> None:
        self.last_command: Optional[str] = None
        self.last_power: Optional[int] = None
        self.soc = 75

    async def get_ac_output_power(self) -> int:
        return 200

    async def start_charge(self) -> int:
        self.last_command = "charge"
        self.last_power = 20
        return 20

    async def start_discharge(self) -> int:
        self.last_command = "discharge"
        self.last_power = 20
        return 20

    async def set_ac_input_limit(self, power_w: int) -> int:
        self.last_command = "input_limit"
        self.last_power = power_w
        return power_w

    async def set_ac_output_limit(self, power_w: int) -> int:
        self.last_command = "output_limit"
        self.last_power = power_w
        return power_w

    async def stop(self) -> bool:
        self.last_command = "stop"
        self.last_power = None
        return True

    async def get_ac_output_limit(self) -> Optional[int]:
        return 800

    async def get_ac_input_limit(self) -> Optional[int]:
        return 400

    async def is_settled(self, *, use_cache: bool = True) -> Optional[bool]:
        return True

    async def get_state(self):
        return type("S", (), {"battery_soc": self.soc, "grid_input_power": 0})()

    # SolarFlowBase compat for status reporter
    async def get_battery_soc(self, *, use_cache: bool = True) -> Optional[int]:
        return self.soc

    # _setpoint_w compat for _map_mode in status_reporter
    _setpoint_w: int = 200


class FakeGrid:
    async def get_phase_powers(self) -> tuple[float, float, float]:
        return (50.0, 30.0, 20.0)

    async def get_total_power(self) -> float:
        return 100.0


def _make_zfi_plan_payload(device_id: str, dt_hours: float = 0.25) -> dict:
    """Build a plan payload in GridPythia's publish_plans format.

    Contains 4 ZFI slots starting 15 min ago, so the first slot is active now.
    """
    now_utc = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    slot_s = dt_hours * 3600
    steps = []
    for i in range(4):
        ts = now_utc - timedelta(seconds=slot_s) + timedelta(seconds=slot_s * i)
        steps.append(
            {
                "timestamp": ts.isoformat(),
                "mode": 2,  # DISCHARGE_ZERO_FEED_IN
                "mode_name": "DISCHARGE_ZERO_FEED_IN",
                "charge_ac_wh": 0.0,
                "discharge_ac_wh": 200.0,  # 200 Wh / 0.25 h = 800 W
                "pv_to_ac_wh": 0.0,
                "pv_to_battery_wh": 0.0,
                "battery_soc_wh": None,
            }
        )
    return {
        "device_id": device_id,
        "published_at": now_utc.isoformat(),
        "dt_hours": dt_hours,
        "steps": steps,
    }


def _make_mixed_plan_payload(device_id: str, dt_hours: float = 0.25) -> dict:
    """Plan: 2x ZFI (active now), 2x IDLE, 2x AC_CHARGE."""
    now_utc = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    slot_s = dt_hours * 3600
    slots = [
        (2, "DISCHARGE_ZERO_FEED_IN", 200.0, 0.0),  # ZFI now
        (2, "DISCHARGE_ZERO_FEED_IN", 200.0, 0.0),  # ZFI +15min
        (0, "IDLE", 0.0, 0.0),
        (0, "IDLE", 0.0, 0.0),
        (3, "AC_CHARGE", 0.0, 150.0),  # AC_CHARGE +1h
        (3, "AC_CHARGE", 0.0, 150.0),
    ]
    steps = []
    for i, (mode, name, dis, chg) in enumerate(slots):
        ts = now_utc - timedelta(seconds=slot_s) + timedelta(seconds=slot_s * i)
        steps.append(
            {
                "timestamp": ts.isoformat(),
                "mode": mode,
                "mode_name": name,
                "charge_ac_wh": chg,
                "discharge_ac_wh": dis,
                "pv_to_ac_wh": 0.0,
                "pv_to_battery_wh": 0.0,
                "battery_soc_wh": None,
            }
        )
    return {
        "device_id": device_id,
        "published_at": now_utc.isoformat(),
        "dt_hours": dt_hours,
        "steps": steps,
    }


# ── Helper: publish via raw paho ──────────────────────────────────────────────


def _publish_plan(payload: dict) -> None:
    """Synchronously publish a plan message to localhost MQTT."""
    import paho.mqtt.publish as publish  # type: ignore[import-untyped]

    publish.single(
        PLAN_TOPIC,
        json.dumps(payload),
        hostname="127.0.0.1",
        port=1883,
        retain=True,
        qos=1,
    )


def _clear_retained(topic: str) -> None:
    """Remove a retained message by publishing empty payload."""
    import paho.mqtt.publish as publish  # type: ignore[import-untyped]

    publish.single(topic, payload="", hostname="127.0.0.1", port=1883, retain=True, qos=0)


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestPlanPayloadParsing:
    """Verify InverterPlan can parse the exact format GridPythia publishes."""

    def test_parse_zfi_plan(self) -> None:
        from ZeroPythia.gridpythia_bridge.models import InverterMode, InverterPlan

        payload = _make_zfi_plan_payload(DEVICE_ID)
        plan = InverterPlan.model_validate(payload)

        assert plan.device_id == DEVICE_ID
        assert plan.dt_hours == 0.25
        assert len(plan.steps) == 4
        assert plan.steps[0].mode == InverterMode.DISCHARGE_ZERO_FEED_IN
        assert plan.steps[0].discharge_ac_wh == 200.0
        # timestamp must be timezone-aware datetime
        assert plan.steps[0].timestamp.tzinfo is not None

    def test_parse_mixed_plan(self) -> None:
        from ZeroPythia.gridpythia_bridge.models import InverterMode, InverterPlan

        payload = _make_mixed_plan_payload(DEVICE_ID)
        plan = InverterPlan.model_validate(payload)
        assert plan.steps[0].mode == InverterMode.DISCHARGE_ZERO_FEED_IN
        assert plan.steps[2].mode == InverterMode.IDLE
        assert plan.steps[4].mode == InverterMode.AC_CHARGE

    def test_get_current_step_returns_zfi(self) -> None:
        from ZeroPythia.gridpythia_bridge.models import InverterMode, InverterPlan

        payload = _make_zfi_plan_payload(DEVICE_ID)
        plan = InverterPlan.model_validate(payload)
        now = datetime.now(tz=timezone.utc)
        step = plan.get_current_step(now)
        assert step is not None
        assert step.mode == InverterMode.DISCHARGE_ZERO_FEED_IN

    def test_plan_summary_merges_slots(self) -> None:
        from ZeroPythia.gridpythia_bridge.models import InverterPlan
        from ZeroPythia.runtime.auto_mode import build_plan_summary

        payload = _make_mixed_plan_payload(DEVICE_ID)
        plan = InverterPlan.model_validate(payload)
        now = datetime.now(tz=timezone.utc)
        summary = build_plan_summary(plan, now)

        # 3 merged groups: ZFI, IDLE, AC_CHARGE
        labels = [e.mode_label for e in summary]
        assert labels == ["Zero-Feed", "Idle", "AC Charge"], f"Got: {labels}"
        # ZFI/TFI discharge value is intentionally hidden in summary.
        zfi_entry = summary[0]
        assert zfi_entry.power_w is None


@pytest.mark.asyncio
class TestAutoModeManagerMqtt:
    """Integration tests with a real MQTT broker on localhost:1883."""

    async def test_plan_received_and_dispatches_zero_feed(self) -> None:
        """Publish ZFI plan → AutoModeManager should dispatch DISCHARGE_ZERO_FEED."""
        from ZeroPythia.runtime.auto_mode import AutoModeManager

        battery = FakeBattery()
        dispatched: list[tuple] = []

        async def mock_apply_cb(mode, charge_w, max_dis_w):
            dispatched.append((mode, charge_w, max_dis_w))

        manager = AutoModeManager(
            mqtt_broker="mqtt://127.0.0.1:1883",
            device_id=DEVICE_ID,
            battery=battery,
            config_max_w=800,
            config_min_w=20,
            topic_prefix=TOPIC_PREFIX,
            status_interval_s=999,  # don't fire during test
        )

        try:
            manager.start()
            # Publish a ZFI plan so the broker delivers it on subscribe
            _publish_plan(_make_zfi_plan_payload(DEVICE_ID))

            # Allow paho to receive the retained plan (runs in its own thread)
            deadline = time.monotonic() + 5.0
            while not manager._subscriber.has_plan and time.monotonic() < deadline:
                await asyncio.sleep(0.1)

            assert manager._subscriber.has_plan, "Plan not received within 5 s"

            # Tick the manager once → should dispatch ZFI
            await manager.tick(mock_apply_cb)

            assert len(dispatched) >= 1
            from ZeroPythia.runtime.models import DeviceMode

            mode, charge_w, max_dis_w = dispatched[0]
            assert mode == DeviceMode.DISCHARGE_ZERO_FEED
            assert max_dis_w == 800  # capped to config_max_w
        finally:
            manager.stop()
            _clear_retained(PLAN_TOPIC)

    async def test_no_plan_falls_back_to_zero_feed(self) -> None:
        """No plan → AutoModeManager falls back to DISCHARGE_ZERO_FEED."""
        from ZeroPythia.runtime.auto_mode import AutoModeManager
        from ZeroPythia.runtime.models import DeviceMode

        battery = FakeBattery()
        dispatched: list[tuple] = []

        async def mock_apply_cb(mode, charge_w, max_dis_w):
            dispatched.append((mode, charge_w, max_dis_w))

        manager = AutoModeManager(
            mqtt_broker="mqtt://127.0.0.1:1883",
            device_id=DEVICE_ID + "_noplan",
            battery=battery,
            config_max_w=600,
            config_min_w=20,
            topic_prefix=TOPIC_PREFIX,
            status_interval_s=999,
        )

        try:
            manager.start()
            await asyncio.sleep(0.3)  # allow paho to connect; no retained plan

            await manager.tick(mock_apply_cb)

            assert len(dispatched) == 1
            mode, _, max_dis_w = dispatched[0]
            assert mode == DeviceMode.DISCHARGE_ZERO_FEED
            assert max_dis_w == 600
        finally:
            manager.stop()

    async def test_mixed_plan_dispatches_correct_sequence(self) -> None:
        """ZFI→IDLE→AC_CHARGE plan steps dispatch the right modes."""
        from ZeroPythia.runtime.auto_mode import AutoModeManager
        from ZeroPythia.runtime.models import DeviceMode
        from ZeroPythia.gridpythia_bridge.models import InverterMode, InverterPlan

        battery = FakeBattery()
        dispatched: list[tuple] = []

        async def mock_apply_cb(mode, charge_w, max_dis_w):
            dispatched.append((mode, charge_w, max_dis_w))

        manager = AutoModeManager(
            mqtt_broker="mqtt://127.0.0.1:1883",
            device_id=DEVICE_ID,
            battery=battery,
            config_max_w=800,
            config_min_w=20,
            topic_prefix=TOPIC_PREFIX,
            status_interval_s=999,
        )

        try:
            manager.start()
            payload = _make_mixed_plan_payload(DEVICE_ID)
            _publish_plan(payload)

            deadline = time.monotonic() + 5.0
            while not manager._subscriber.has_plan and time.monotonic() < deadline:
                await asyncio.sleep(0.1)

            assert manager._subscriber.has_plan

            # Tick 1: active step = ZFI → dispatch DISCHARGE_ZERO_FEED
            await manager.tick(mock_apply_cb)
            assert dispatched[-1][0] == DeviceMode.DISCHARGE_ZERO_FEED

            # Manually simulate step switch to IDLE
            plan = manager._subscriber._plan
            assert plan is not None
            # Force-switch by direct manipulation
            manager._last_inv_mode = InverterMode.DISCHARGE_ZERO_FEED_IN

            # Inject an IDLE step as "current"
            idle_step = next(s for s in plan.steps if s.mode == InverterMode.IDLE)
            await manager._apply_step(idle_step, mock_apply_cb)
            assert dispatched[-1][0] == DeviceMode.IDLE

            # Inject an AC_CHARGE step
            charge_step = next(s for s in plan.steps if s.mode == InverterMode.AC_CHARGE)
            await manager._apply_step(charge_step, mock_apply_cb)
            assert dispatched[-1][0] == DeviceMode.AC_CHARGE
            assert dispatched[-1][1] == 600  # 150 Wh / 0.25 h

        finally:
            manager.stop()
            _clear_retained(PLAN_TOPIC)


@pytest.mark.asyncio
class TestControlRuntimeAutoMode:
    """ControlRuntime integration: AUTO mode routes samples and dispatches via AutoModeManager."""

    async def test_runtime_auto_mode_sets_effective_mode(self) -> None:
        """Activating AUTO → first tick should set effective DISCHARGE_ZERO_FEED."""
        from ZeroPythia.runtime.auto_mode import AutoModeManager
        from ZeroPythia.runtime.models import DeviceMode
        from ZeroPythia.runtime.control_runtime import ControlRuntime

        battery = FakeBattery()
        grid = FakeGrid()
        runtime = ControlRuntime(
            grid_meter=grid,
            battery=cast(Any, battery),
            sampling_interval_s=0.1,
            control_interval_s=0.5,
            max_discharge_w=800,
            min_discharge_w=20,
        )

        _publish_plan(_make_zfi_plan_payload(DEVICE_ID))

        manager = AutoModeManager(
            mqtt_broker="mqtt://127.0.0.1:1883",
            device_id=DEVICE_ID,
            battery=battery,
            config_max_w=800,
            config_min_w=20,
            topic_prefix=TOPIC_PREFIX,
            status_interval_s=999,
        )

        try:
            manager.start()
            runtime.attach_auto_mode_manager(manager)
            await runtime.set_mode(DeviceMode.AUTO)
            await runtime.start()

            # Wait until plan arrives, is summarised, AND effective mode dispatched from plan
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                await asyncio.sleep(0.2)
                auto_st = runtime.get_state().auto_status
                if (
                    auto_st is not None
                    and auto_st.has_plan
                    and bool(auto_st.plan_summary)
                    and runtime._auto_effective_mode == DeviceMode.DISCHARGE_ZERO_FEED
                ):
                    break

            assert runtime._auto_effective_mode == DeviceMode.DISCHARGE_ZERO_FEED, (
                f"Effective mode is still {runtime._auto_effective_mode}"
            )
            # The DashboardState should have auto_status populated
            state = runtime.get_state()
            assert state.mode == DeviceMode.AUTO
            assert state.auto_status is not None
            assert state.auto_status.has_plan is True
            assert state.auto_status.plan_summary  # non-empty

        finally:
            await runtime.stop()
            manager.stop()
            _clear_retained(PLAN_TOPIC)

    async def test_status_reporter_publishes_to_mqtt(self) -> None:
        """Status reporter should publish {soc, mode} to the status topic."""
        import paho.mqtt.client as mqtt  # type: ignore[import-untyped]

        battery = FakeBattery()
        received: list[dict] = []

        # Subscribe to status topic with a raw paho client
        sub_client = mqtt.Client(
            client_id="test-sub",
            clean_session=True,
            protocol=mqtt.MQTTv311,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )

        def on_message(client, userdata, msg):
            try:
                received.append(json.loads(msg.payload))
            except Exception:
                pass

        sub_client.on_message = on_message
        sub_client.connect("127.0.0.1", 1883, keepalive=10)
        sub_client.subscribe(STATUS_TOPIC, qos=0)
        sub_client.loop_start()

        from ZeroPythia.runtime.auto_mode import AutoModeManager

        manager = AutoModeManager(
            mqtt_broker="mqtt://127.0.0.1:1883",
            device_id=DEVICE_ID,
            battery=battery,
            config_max_w=800,
            config_min_w=20,
            topic_prefix=TOPIC_PREFIX,
            status_interval_s=0.5,  # fire quickly for test
        )

        try:
            manager.start()
            await manager.start_reporter_task()

            deadline = time.monotonic() + 5.0
            while not received and time.monotonic() < deadline:
                await asyncio.sleep(0.1)

            assert received, "Status reporter did not publish within 5 s"
            msg = received[0]
            assert "soc" in msg, f"Missing 'soc' in {msg}"
            assert "mode" in msg, f"Missing 'mode' in {msg}"
            assert msg["soc"] == 75
            assert msg["mode"] in (0, 1, 2, 3, 4)  # valid InverterMode
        finally:
            await manager.stop_reporter_task()
            manager.stop()
            sub_client.loop_stop()
            sub_client.disconnect()

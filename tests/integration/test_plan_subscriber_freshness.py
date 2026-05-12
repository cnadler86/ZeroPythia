from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, cast

from ZeroPythia.gridpythia.plan_subscriber import GridPythiaPlanSubscriber


class DummyMqttClient:
    def subscribe(self, topic: str, callback):
        self.topic = topic
        self.callback = callback


def _payload(*, published_at: datetime, step_mode: int) -> dict:
    now = datetime.now(tz=timezone.utc).replace(second=0, microsecond=0)
    return {
        "device_id": "DEV1",
        "published_at": published_at.isoformat(),
        "dt_hours": 1.0,
        "steps": [
            {
                "timestamp": (now - timedelta(minutes=10)).isoformat(),
                "mode": step_mode,
                "mode_name": "X",
                "charge_ac_wh": 0.0,
                "discharge_ac_wh": 0.0,
                "pv_to_ac_wh": 0.0,
                "pv_to_battery_wh": 0.0,
                "battery_soc_wh": None,
            }
        ],
    }


def test_older_plan_is_ignored() -> None:
    mqtt = DummyMqttClient()
    sub = GridPythiaPlanSubscriber(
        mqtt_client=cast(Any, mqtt), device_id="DEV1", topic_prefix="gridpythia"
    )

    newer_ts = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    older_ts = datetime(2026, 4, 26, 11, 0, 0, tzinfo=timezone.utc)

    # First receive newer plan
    sub._on_message("gridpythia/inverters/DEV1/plan", _payload(published_at=newer_ts, step_mode=0))
    # Then receive older plan (must be ignored)
    sub._on_message("gridpythia/inverters/DEV1/plan", _payload(published_at=older_ts, step_mode=3))

    assert sub._plan is not None  # noqa: SLF001
    assert sub._plan.published_at == newer_ts  # noqa: SLF001


def test_newer_plan_replaces_current() -> None:
    mqtt = DummyMqttClient()
    sub = GridPythiaPlanSubscriber(
        mqtt_client=cast(Any, mqtt), device_id="DEV1", topic_prefix="gridpythia"
    )

    older_ts = datetime(2026, 4, 26, 11, 0, 0, tzinfo=timezone.utc)
    newer_ts = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)

    sub._on_message("gridpythia/inverters/DEV1/plan", _payload(published_at=older_ts, step_mode=0))
    sub._on_message("gridpythia/inverters/DEV1/plan", _payload(published_at=newer_ts, step_mode=3))

    assert sub._plan is not None  # noqa: SLF001
    assert sub._plan.published_at == newer_ts  # noqa: SLF001

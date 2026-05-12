"""Periodically reports battery SoC and current mode to GridPythia via MQTT.

Reads the Zendure device state, maps the AC mode to the closest GridPythia
``InverterMode``, and publishes::

    Topic:   gridpythia/inverters/{device_id}/status
    Payload: {"soc": 63.5, "mode": 2}

GridPythia's ``MqttGateway`` receives this and updates the
``InverterCoordinator``, keeping the optimizer informed about the real
battery state.

The reporter runs as an asyncio task and can be started / stopped cleanly::

    reporter = GridPythiaStatusReporter(
        mqtt_client=client,
        battery=solarflow,
        device_id="SF800Pro",
        topic_prefix="gridpythia",
        interval_s=60,
    )
    task = asyncio.create_task(reporter.run())
    ...
    task.cancel()
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from clients.mqtt.client import MqttClient
from clients.zendure.base import BatteryManager
from ZeroPythia.gridpythia.models import InverterMode

logger = logging.getLogger(__name__)


def _map_mode(battery: BatteryManager) -> InverterMode:
    """Map the last-known Zendure setpoint to the closest GridPythia InverterMode.

    We use the internal ``_setpoint_w`` field as the source
    of truth rather than reading the device (avoids an extra HTTP round-trip).

    * setpoint > 0  → discharging  → ``DISCHARGE_ZERO_FEED_IN`` (safest default)
    * setpoint < 0  → charging     → ``AC_CHARGE``
    * setpoint == 0 → idle/stopped → ``IDLE``
    """
    sp = getattr(battery, "_setpoint_w", 0)
    if sp > 0:
        return InverterMode.DISCHARGE_ZERO_FEED_IN
    if sp < 0:
        return InverterMode.AC_CHARGE
    return InverterMode.IDLE


class GridPythiaStatusReporter:
    """Asyncio task that publishes battery status to GridPythia every *interval_s* seconds."""

    def __init__(
        self,
        mqtt_client: MqttClient,
        battery: BatteryManager,
        device_id: str,
        topic_prefix: str = "gridpythia",
        interval_s: float = 60.0,
    ) -> None:
        self._mqtt = mqtt_client
        self._battery = battery
        self._device_id = device_id
        self._topic = f"{topic_prefix}/inverters/{device_id}/status"
        self._interval_s = interval_s

    async def run(self) -> None:
        """Run the reporter loop until cancelled."""
        logger.info(
            "status_reporter_started",
            extra={"topic": self._topic, "interval_s": self._interval_s},
        )
        try:
            while True:
                await self._report_once()
                await asyncio.sleep(self._interval_s)
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("status_reporter_stopped", extra={"device_id": self._device_id})

    async def _report_once(self) -> None:
        """Read SoC from device and publish status."""
        try:
            soc: Optional[int] = await self._battery.get_battery_soc()
        except Exception as exc:
            logger.warning(
                "status_reporter_soc_error",
                extra={"device_id": self._device_id, "error": str(exc)},
            )
            return

        if soc is None:
            logger.debug("status_reporter_soc_none", extra={"device_id": self._device_id})
            return

        mode = _map_mode(self._battery)
        payload = {"soc": float(soc), "mode": int(mode)}

        if not self._mqtt.is_connected:
            logger.debug(
                "status_reporter_mqtt_not_connected",
                extra={"device_id": self._device_id},
            )
            return

        self._mqtt.publish(self._topic, payload)
        logger.debug(
            "status_reported",
            extra={"device_id": self._device_id, "soc": soc, "mode": mode.name},
        )

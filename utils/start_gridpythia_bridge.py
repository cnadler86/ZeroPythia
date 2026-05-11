"""GridPythia Bridge – report status + execute plan.

Connects Zendure SolarFlow with GridPythia via MQTT:

  1. Status reporter (every 60 s):
       Reads SoC from the Zendure device and publishes it to GridPythia:
       → gridpythia/inverters/{device_id}/status  {"soc": 63.5, "mode": 2}

  2. Plan executor (every 60 s):
       Receives the optimisation plan from GridPythia and sets the battery
       according to the current slot:

       IDLE (0)                  → battery.stop()
       DISCHARGE (1)             → battery.start_discharge(plan_w)
       DISCHARGE_ZERO_FEED_IN(2) → battery.start_discharge(plan_w)
       AC_CHARGE (3+4)           → battery.start_charge(plan_charge_w)
       No plan / stale           → battery.start_discharge(fallback_w)

No zero-feed control loop – the battery simply follows the plan.

Usage:
    python utils/start_gridpythia_bridge.py
    python utils/start_gridpythia_bridge.py --zendure 192.168.178.140 --device-id SF800Pro
    python utils/start_gridpythia_bridge.py --mqtt-broker mqtt://192.168.1.5:1883 --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Ensure the repo root is in sys.path when running the script directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients.mqtt.client import MqttClient, MqttConfig
from clients.zendure.aiozen import SolarFlowAsyncClient
from clients.zendure.base import SolarFlowBase
from src.gridpythia.models import InverterMode, PlanStep
from src.gridpythia.plan_subscriber import GridPythiaPlanSubscriber
from src.gridpythia.status_reporter import GridPythiaStatusReporter

LOG = logging.getLogger("gridpythia_bridge")


# ── Plan-Executor (kein ZeroFeed-Loop) ──────────────────────────────────────


class SimplePlanExecutor:
    """Executes GridPythia plan steps directly as battery commands.

    Called every *check_interval_s* seconds.  When no valid plan is present,
    the battery falls back to *fallback_discharge_w* discharge.
    """

    def __init__(
        self,
        battery: SolarFlowBase,
        plan_subscriber: GridPythiaPlanSubscriber,
        device_id: str,
        check_interval_s: float = 60.0,
    ) -> None:
        self._battery = battery
        self._subscriber = plan_subscriber
        self._device_id = device_id
        self._interval = check_interval_s
        self._active_mode: Optional[InverterMode] = None

    async def run(self) -> None:
        LOG.info(
            "Plan executor started (device_id=%s, interval=%.0f s)",
            self._device_id,
            self._interval,
        )
        try:
            while True:
                await self._tick()
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            pass
        finally:
            LOG.info("Plan executor stopped")

    async def _tick(self) -> None:
        now = datetime.now(tz=timezone.utc)
        step = self._subscriber.get_current_step(now)

        if step is None:
            await self._apply_fallback()
        else:
            await self._apply_step(step)

    async def _apply_fallback(self) -> None:
        if self._active_mode == InverterMode.IDLE:
            return  # already stopped
        LOG.info("No active plan → stop")
        try:
            await self._battery.stop()
            self._active_mode = InverterMode.IDLE
        except Exception:
            LOG.exception("Error during fallback stop()")

    async def _apply_step(self, step: PlanStep) -> None:
        mode = step.mode
        plan = self._subscriber._plan
        dt_h = plan.dt_hours if plan is not None else 0.25

        if mode == InverterMode.IDLE:
            if self._active_mode == InverterMode.IDLE:
                return
            LOG.info("Plan mode: IDLE → stop")
            try:
                await self._battery.stop()
                self._active_mode = InverterMode.IDLE
            except Exception:
                LOG.exception("Error in stop()")

        elif mode in (InverterMode.DISCHARGE, InverterMode.DISCHARGE_ZERO_FEED_IN):
            plan_w = int(step.discharge_ac_wh / dt_h) if dt_h > 0 else 0
            plan_w = max(1, plan_w)
            if self._active_mode == mode:
                return
            LOG.info("Plan mode: %s → start_discharge(%d W)", mode.name, plan_w)
            try:
                setpoint = await self._battery.start_discharge()
                if plan_w > setpoint:
                    await self._battery.set_ac_output_limit(plan_w)
                self._active_mode = mode
            except Exception:
                LOG.exception("Error in start_discharge()")

        elif mode in (InverterMode.AC_CHARGE, InverterMode.AC_CHARGE_ZERO_FEED_IN):
            plan_charge_w = int(step.charge_ac_wh / dt_h) if dt_h > 0 else 0
            if self._active_mode == mode:
                return
            if plan_charge_w <= 0:
                LOG.info("Plan mode: %s with 0 Wh → stop", mode.name)
                try:
                    await self._battery.stop()
                    self._active_mode = InverterMode.IDLE
                except Exception:
                    LOG.exception("Error in stop()")
            else:
                LOG.info("Plan mode: %s → start_charge(%d W)", mode.name, plan_charge_w)
                try:
                    setpoint = await self._battery.start_charge()
                    if plan_charge_w > setpoint:
                        await self._battery.set_ac_input_limit(plan_charge_w)
                    self._active_mode = mode
                except Exception:
                    LOG.exception("Error in start_charge()")


# ── Hauptfunktion ────────────────────────────────────────────────────────────


async def run(
    zendure_ip: str,
    device_id: str,
    mqtt_broker: str,
    topic_prefix: str,
    status_interval_s: float,
) -> None:
    mqtt_cfg = MqttConfig(
        broker=mqtt_broker,
        client_id=f"zerofeed-bridge-{device_id}",
        topic_prefix=topic_prefix,
    )
    mqtt_client = MqttClient(mqtt_cfg)

    plan_subscriber = GridPythiaPlanSubscriber(
        mqtt_client=mqtt_client,
        device_id=device_id,
        topic_prefix=topic_prefix,
    )
    plan_subscriber.register()  # must be registered before mqtt_client.start()

    async with SolarFlowAsyncClient(zendure_ip) as solarflow:
        # Status-Reporter
        reporter = GridPythiaStatusReporter(
            mqtt_client=mqtt_client,
            battery=solarflow,
            device_id=device_id,
            topic_prefix=topic_prefix,
            interval_s=status_interval_s,
        )

        # Plan-Executor
        executor = SimplePlanExecutor(
            battery=solarflow,
            plan_subscriber=plan_subscriber,
            device_id=device_id,
            check_interval_s=status_interval_s,
        )

        mqtt_client.start()
        LOG.info(
            "GridPythia Bridge started — Zendure=%s  device_id=%s  broker=%s",
            zendure_ip,
            device_id,
            mqtt_broker,
        )

        tasks = [
            asyncio.create_task(reporter.run(), name="status-reporter"),
            asyncio.create_task(executor.run(), name="plan-executor"),
        ]

        # Execute first tick immediately (don't wait 60 s)
        await executor._tick()

        try:
            # return_exceptions=True: task crashes don't kill the whole bridge
            done = await asyncio.gather(*tasks, return_exceptions=True)
            for result in done:
                if isinstance(result, Exception):
                    LOG.error("Task error: %s", result, exc_info=result)
        except asyncio.CancelledError:
            pass
        finally:
            LOG.info("Stopping bridge…")
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            mqtt_client.stop()
            LOG.info("Bridge stopped")


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="GridPythia Bridge – Status reporten + Plan ausführen (kein ZeroFeed-Loop)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--zendure",
        default="192.168.178.140",
        metavar="IP",
        help="Zendure SolarFlow IP-Adresse",
    )
    parser.add_argument(
        "--device-id",
        default="SF800Pro",
        metavar="ID",
        help="Inverter device_id wie in der GridPythia config.yaml",
    )
    parser.add_argument(
        "--mqtt-broker",
        default="mqtt://localhost:1883",
        metavar="URL",
        help="MQTT Broker URL",
    )
    parser.add_argument(
        "--topic-prefix",
        default="gridpythia",
        metavar="PREFIX",
        help="MQTT Topic-Prefix (muss mit GridPythia server.mqtt.topic_prefix übereinstimmen)",
    )
    parser.add_argument(
        "--status-interval",
        type=float,
        default=60.0,
        metavar="S",
        help="Sekunden zwischen Status-Reports und Plan-Checks",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Debug-Logging aktivieren",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    try:
        asyncio.run(
            run(
                zendure_ip=args.zendure,
                device_id=args.device_id,
                mqtt_broker=args.mqtt_broker,
                topic_prefix=args.topic_prefix,
                status_interval_s=args.status_interval,
            )
        )
    except KeyboardInterrupt:
        LOG.info("Durch Benutzer beendet")


if __name__ == "__main__":
    main()

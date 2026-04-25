"""GridPythia Bridge – Status reporten + Plan ausführen.

Verbindet Zendure SolarFlow mit GridPythia über MQTT:

  1. Status-Reporter (alle 60 s):
       Liest SoC vom Zendure-Gerät und publiziert ihn an GridPythia:
       → gridpythia/inverters/{device_id}/status  {"soc": 63.5, "mode": 2}

  2. Plan-Executor (alle 60 s):
       Empfängt den Optimierungsplan von GridPythia und setzt die Batterie
       entsprechend dem aktuellen Slot:

       IDLE (0)                  → battery.stop()
       DISCHARGE (1)             → battery.start_discharge(plan_w)
       DISCHARGE_ZERO_FEED_IN(2) → battery.start_discharge(plan_w)
       AC_CHARGE (3+4)           → battery.start_charge(plan_charge_w)
       Kein Plan / veraltet      → battery.start_discharge(fallback_w)

Kein Zero-Feed-Regelkreis – die Batterie folgt einfach dem Plan.

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
    """Führt GridPythia-Plan-Schritte direkt als Batterie-Befehle aus.

    Wird alle *check_interval_s* Sekunden ausgeführt.  Wenn kein gültiger
    Plan vorhanden ist, läuft die Batterie auf *fallback_discharge_w* Entladung.
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
            "Plan-Executor gestartet (device_id=%s, interval=%.0fs)",
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
            LOG.info("Plan-Executor gestoppt")

    async def _tick(self) -> None:
        now = datetime.now(tz=timezone.utc)
        step = self._subscriber.get_current_step(now)

        if step is None:
            await self._apply_fallback()
        else:
            await self._apply_step(step)

    async def _apply_fallback(self) -> None:
        if self._active_mode == InverterMode.IDLE:
            return  # bereits gestoppt
        LOG.info("Kein aktiver Plan → stop")
        try:
            await self._battery.stop()
            self._active_mode = InverterMode.IDLE
        except Exception:
            LOG.exception("Fehler beim Fallback-stop()")

    async def _apply_step(self, step: PlanStep) -> None:
        mode = step.mode
        plan = self._subscriber._plan
        dt_h = plan.dt_hours if plan is not None else 0.25

        if mode == InverterMode.IDLE:
            if self._active_mode == InverterMode.IDLE:
                return
            LOG.info("Plan-Modus: IDLE → stop")
            try:
                await self._battery.stop()
                self._active_mode = InverterMode.IDLE
            except Exception:
                LOG.exception("Fehler bei stop()")

        elif mode in (InverterMode.DISCHARGE, InverterMode.DISCHARGE_ZERO_FEED_IN):
            plan_w = int(step.discharge_ac_wh / dt_h) if dt_h > 0 else 0
            plan_w = max(1, plan_w)
            if self._active_mode == mode:
                return
            LOG.info("Plan-Modus: %s → start_discharge(%dW)", mode.name, plan_w)
            try:
                await self._battery.start_discharge(plan_w)
                self._active_mode = mode
            except Exception:
                LOG.exception("Fehler bei start_discharge()")

        elif mode in (InverterMode.AC_CHARGE, InverterMode.AC_CHARGE_ZERO_FEED_IN):
            plan_charge_w = int(step.charge_ac_wh / dt_h) if dt_h > 0 else 0
            if self._active_mode == mode:
                return
            if plan_charge_w <= 0:
                LOG.info("Plan-Modus: %s mit 0 Wh → stop", mode.name)
                try:
                    await self._battery.stop()
                    self._active_mode = InverterMode.IDLE
                except Exception:
                    LOG.exception("Fehler bei stop()")
            else:
                LOG.info("Plan-Modus: %s → start_charge(%dW)", mode.name, plan_charge_w)
                try:
                    await self._battery.start_charge(plan_charge_w)
                    self._active_mode = mode
                except Exception:
                    LOG.exception("Fehler bei start_charge()")


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
    plan_subscriber.register()  # vor mqtt_client.start() registrieren

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
            "GridPythia Bridge gestartet — Zendure=%s  device_id=%s  broker=%s",
            zendure_ip,
            device_id,
            mqtt_broker,
        )

        tasks = [
            asyncio.create_task(reporter.run(), name="status-reporter"),
            asyncio.create_task(executor.run(), name="plan-executor"),
        ]

        # Ersten Tick sofort ausführen (nicht 60 s warten)
        await executor._tick()

        try:
            # return_exceptions=True: task crashes don't kill the whole bridge
            done = await asyncio.gather(*tasks, return_exceptions=True)
            for result in done:
                if isinstance(result, Exception):
                    LOG.error("Task-Fehler: %s", result, exc_info=result)
        except asyncio.CancelledError:
            pass
        finally:
            LOG.info("Bridge wird gestoppt …")
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            mqtt_client.stop()
            LOG.info("Bridge gestoppt")


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

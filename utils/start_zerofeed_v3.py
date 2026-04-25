"""ZeroFeed V3 – Startscript für den echten Betrieb.

Startet den phasen-bewussten Zero-Feed Controller V3 mit
echtem Shelly 3EM und Zendure SolarFlow.

Loggt alle Messwerte und Controller-Interna als CSV (pro Tag):
    <log_dir>/zerofeed_v3_YYYY-MM-DD.csv

Zeilentypen:
    sample  – Shelly-Messwerte + Oszillationsdetektor-Zustand (~1s)
    control – Regler-Ausgaben + Setpoint (~1s)

GridPythia-Integration (optional, via --mqtt-broker):
    - Meldet SoC + Mode alle 60 s an GridPythia
    - Empfängt Optimierungsplan und führt ihn aus
    - Fallback auf Zero-Feed wenn kein Plan verfügbar

Usage:
    python utils/start_zerofeed_v3.py
    python utils/start_zerofeed_v3.py --shelly 192.168.1.10 --zendure 192.168.1.20
    python utils/start_zerofeed_v3.py --mqtt-broker mqtt://192.168.1.5:1883 --device-id SF800Pro
    python utils/start_zerofeed_v3.py --log-dir /pfad/zu/logs --verbose
"""

import argparse
import asyncio
import logging
from pathlib import Path
from typing import Optional

from clients.shelly.shelly import ShellyClient
from clients.zendure.aiozen import SolarFlowAsyncClient
from src.controller.csv_logger import ZeroFeedCSVLogger
from src.controller.oscillation_detectorv2 import BaseloadHolderSettings
from src.controller.phase_controller import (
    InverterPhaseControllerSettings,
    PhaseControllerSettings,
    ZeroFeedManagerSettings,
)
from src.controller.zerofeed_v3 import ZeroFeedV3Controller, ZeroFeedV3Settings

LOG = logging.getLogger("start_zerofeed_v3")


# ── Shelly-Adapter ──────────────────────────────────────────────────────────


class ShellyGridMeter:
    """Adapter: ShellyClient → GridMeter-Protocol für ZeroFeedV3Controller.

    Implementiert get_phase_powers() und get_total_power() aus dem
    ZeroFeedV3 GridMeter-Protocol.
    """

    def __init__(self, client: ShellyClient):
        self._client = client

    async def get_phase_powers(self) -> Optional[tuple[float, float, float]]:
        state = await self._client.get_state()
        if state is None:
            return None
        return (state.phase_a_power_w, state.phase_b_power_w, state.phase_c_power_w)

    async def get_total_power(self) -> Optional[float]:
        state = await self._client.get_state(use_cache=True)
        return state.total_power_w if state is not None else None


# ── Hauptfunktion ────────────────────────────────────────────────────────────


async def run(
    shelly_ip: str,
    zendure_ip: str,
    log_dir: Path,
    max_output: int,
    min_discharge: int,
    kp_draw: float,
    kp_feed_in: float,
    control_interval: float,
    mqtt_broker: Optional[str],
    device_id: str,
    topic_prefix: str,
    status_interval_s: float,
    mode: str = "ff_plus_b",
) -> None:
    # ── Controller-Modus konfigurieren ──────────────────────────────────
    # ff_only:    Nur Feedforward (A+C), kein Feedback auf Phase B
    # ff_plus_b:  Feedforward + Feedback Phase B, keine Oszilationserkennung
    # ff_osc_ac:  + Oszilationserkennung auf Phase A und C
    # full:       + Oszilationserkennung auf Phase B
    feedback_enabled = mode != "ff_only"
    holder_ac = BaseloadHolderSettings() if mode in ("ff_osc_ac", "full") else None
    holder_b = BaseloadHolderSettings() if mode == "full" else None

    LOG.info(
        "Modus: %s  (feedback=%s  osc_ac=%s  osc_b=%s)",
        mode,
        feedback_enabled,
        holder_ac is not None,
        holder_b is not None,
    )

    settings = ZeroFeedV3Settings(
        manager=ZeroFeedManagerSettings(
            max_output_w=max_output,
            min_output_w=min_discharge,
        ),
        inverter_controller=InverterPhaseControllerSettings(
            kp_draw=kp_draw,
            kp_feed_in=kp_feed_in,
            feedback_enabled=feedback_enabled,
        ),
        phase_controller=PhaseControllerSettings(),
        control_interval_s=control_interval,
        holder_settings_ac=holder_ac,
        holder_settings_b=holder_b,
    )

    csv_logger = ZeroFeedCSVLogger(log_dir)
    LOG.info("CSV-Log Verzeichnis: %s", log_dir.resolve())

    async with (
        ShellyClient(shelly_ip) as shelly_client,
        SolarFlowAsyncClient(zendure_ip) as solarflow,
    ):
        grid_meter = ShellyGridMeter(shelly_client)

        controller = ZeroFeedV3Controller(
            settings=settings,
            grid_meter=grid_meter,
            battery=solarflow,
            csv_logger=csv_logger,
        )

        LOG.info(
            "Starte ZeroFeed V3 — Shelly=%s  Zendure=%s  max=%dW  ctrl=%.1fs",
            shelly_ip,
            zendure_ip,
            max_output,
            control_interval,
        )

        background_tasks: list[asyncio.Task] = []

        if mqtt_broker:
            from clients.mqtt.client import MqttClient, MqttConfig
            from src.gridpythia.plan_executor import PlanExecutor
            from src.gridpythia.plan_subscriber import GridPythiaPlanSubscriber
            from src.gridpythia.status_reporter import GridPythiaStatusReporter

            mqtt_cfg = MqttConfig(
                broker=mqtt_broker,
                client_id=f"zerofeed-{device_id}",
                topic_prefix=topic_prefix,
            )
            mqtt_client = MqttClient(mqtt_cfg)

            plan_subscriber = GridPythiaPlanSubscriber(
                mqtt_client=mqtt_client,
                device_id=device_id,
                topic_prefix=topic_prefix,
            )
            plan_subscriber.register()  # must be before mqtt_client.start()

            mqtt_client.start()
            LOG.info("MQTT verbunden mit %s (device_id=%s)", mqtt_broker, device_id)

            # Status reporter: SoC + mode → GridPythia
            reporter = GridPythiaStatusReporter(
                mqtt_client=mqtt_client,
                battery=solarflow,
                device_id=device_id,
                topic_prefix=topic_prefix,
                interval_s=status_interval_s,
            )
            background_tasks.append(asyncio.create_task(reporter.run(), name="status-reporter"))

            # Plan executor: dispatches GridPythia plan steps to battery
            executor = PlanExecutor(
                battery=solarflow,
                zero_feed_ctrl=controller,
                plan_subscriber=plan_subscriber,
                device_id=device_id,
                config_max_output_w=max_output,
                config_min_output_w=min_discharge,
                check_interval_s=status_interval_s,
            )
            background_tasks.append(asyncio.create_task(executor.run(), name="plan-executor"))

            # The executor starts/stops the zero-feed controller as needed.
            # Trigger the first tick immediately so zero-feed starts without waiting.
            await executor._tick()
        else:
            # No MQTT → run zero-feed directly (legacy behaviour)
            await controller.start()

        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            LOG.info("Fahre ZeroFeed V3 herunter …")
            for task in background_tasks:
                task.cancel()
            if background_tasks:
                await asyncio.gather(*background_tasks, return_exceptions=True)
            if mqtt_broker:
                mqtt_client.stop()
            try:
                await controller.stop()
            except Exception:
                LOG.exception("Fehler beim Stoppen")


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="ZeroFeed V3 – phasen-bewusste Nulleinspeisung mit CSV-Logging",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--shelly",
        default="192.168.178.77",
        metavar="IP",
        help="Shelly 3EM IP-Adresse",
    )
    parser.add_argument(
        "--zendure",
        default="192.168.178.140",
        metavar="IP",
        help="Zendure SolarFlow IP-Adresse",
    )
    parser.add_argument(
        "--log-dir",
        default=None,
        metavar="PFAD",
        help="Verzeichnis für CSV-Logs (Standard: <repo>/logs/zerofeed_v3/)",
    )
    parser.add_argument(
        "--max-output",
        type=int,
        default=800,
        metavar="W",
        help="Maximale Batterie-Ausgangsleistung",
    )
    parser.add_argument(
        "--min-discharge",
        type=int,
        default=20,
        metavar="W",
        help="Minimale Entladeleistung beim Start",
    )
    parser.add_argument(
        "--kp-draw",
        type=float,
        default=0.9,
        metavar="KP",
        help="P-Regler Verstärkung bei Netzbezug",
    )
    parser.add_argument(
        "--kp-feed-in",
        type=float,
        default=1.05,
        metavar="KP",
        help="P-Regler Verstärkung bei Einspeisung",
    )
    parser.add_argument(
        "--control-interval",
        type=float,
        default=3.0,
        metavar="S",
        help="Regelzyklus in Sekunden (3.0 empfohlen)",
    )
    # ── GridPythia MQTT Integration ─────────────────────────────────────
    parser.add_argument(
        "--mqtt-broker",
        default=None,
        metavar="URL",
        help=(
            "MQTT Broker URL für GridPythia-Integration, z.B. mqtt://192.168.1.5:1883. "
            "Wenn nicht gesetzt, läuft der Controller im klassischen Zero-Feed-Modus."
        ),
    )
    parser.add_argument(
        "--device-id",
        default="SF800Pro",
        metavar="ID",
        help="Inverter device_id wie in der GridPythia config.yaml konfiguriert",
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
        help="Interval fur Status-Reports an GridPythia (Sekunden)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Debug-Logging aktivieren",
    )
    parser.add_argument(
        "--mode",
        choices=["ff_only", "ff_plus_b", "ff_osc_ac", "full"],
        default="ff_plus_b",
        metavar="MODE",
        help="ff_only=Feedforward A+C only  ff_plus_b=+Feedback B  ff_osc_ac=+Osc A+C  full=+Osc B",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    if args.log_dir is not None:
        log_dir = Path(args.log_dir)
    else:
        log_dir = Path(__file__).parent / "logs" / "zerofeed_v3"

    try:
        asyncio.run(
            run(
                shelly_ip=args.shelly,
                zendure_ip=args.zendure,
                log_dir=log_dir,
                max_output=args.max_output,
                min_discharge=args.min_discharge,
                kp_draw=args.kp_draw,
                kp_feed_in=args.kp_feed_in,
                control_interval=args.control_interval,
                mqtt_broker=args.mqtt_broker,
                device_id=args.device_id,
                topic_prefix=args.topic_prefix,
                status_interval_s=args.status_interval,
                mode=args.mode,
            )
        )
    except KeyboardInterrupt:
        LOG.info("Durch Benutzer beendet")


if __name__ == "__main__":
    main()

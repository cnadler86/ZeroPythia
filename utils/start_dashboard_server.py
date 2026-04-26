"""Zendure Dashboard Server – Startscript.

Startet den Dashboard-Server mit echtem Shelly 3EM und Zendure SolarFlow.

Features:
  - WebSocket Dashboard GUI auf http://<host>:<port>/
  - Modus-Steuerung: AC Laden, Idle, Zero-Feed Entladung
  - Regler-Auswahl und Konfiguration per GUI
  - Live-Anzeige: Shelly, Batterie, Oszillationserkennung
  - Optional: GridPythia MQTT-Integration

Usage:
    python utils/start_dashboard_server.py
    python utils/start_dashboard_server.py --shelly 192.168.178.77 --zendure 192.168.178.140
    python utils/start_dashboard_server.py --port 8080 --host 0.0.0.0
    python utils/start_dashboard_server.py --mqtt-broker mqtt://192.168.1.5:1883 --device-id SF800Pro
"""

import argparse
import asyncio
import logging
from typing import Optional

import uvicorn

from clients.shelly.shelly import ShellyClient
from clients.zendure.aiozen import SolarFlowAsyncClient
from src.dashboard.models import DeviceMode
from src.dashboard.regulators.v3_adapter import V3RegulatorSettings, ZeroFeedV3Regulator
from src.dashboard.runtime import ControlRuntime
from src.dashboard.server import create_app

LOG = logging.getLogger("start_dashboard")


# ── Shelly adapter ────────────────────────────────────────────────────────────


class ShellyGridMeter:
    """Adapter: ShellyClient → GridMeterProtocol."""

    def __init__(self, client: ShellyClient) -> None:
        self._client = client

    async def get_phase_powers(self) -> Optional[tuple[float, float, float]]:
        state = await self._client.get_state()
        if state is None:
            return None
        return (state.phase_a_power_w, state.phase_b_power_w, state.phase_c_power_w)

    async def get_total_power(self) -> Optional[float]:
        state = await self._client.get_state(use_cache=True)
        return state.total_power_w if state is not None else None


# ── Main ──────────────────────────────────────────────────────────────────────


async def run(
    shelly_ip: str,
    zendure_ip: str,
    host: str,
    port: int,
    max_output: int,
    min_discharge: int,
    kp_draw: float,
    kp_feed_in: float,
    control_interval: float,
    mqtt_broker: Optional[str],
    device_id: str,
    topic_prefix: str,
    status_interval_s: float,
    initial_mode: str,
) -> None:
    async with (
        ShellyClient(shelly_ip) as shelly_client,
        SolarFlowAsyncClient(zendure_ip) as solarflow,
    ):
        grid_meter = ShellyGridMeter(shelly_client)

        # ── Runtime ───────────────────────────────────────────────────────────
        runtime = ControlRuntime(
            grid_meter=grid_meter,
            battery=solarflow,
            sampling_interval_s=1.0,
            control_interval_s=control_interval,
            max_discharge_w=max_output,
            min_discharge_w=min_discharge,
        )

        # ── Register regulators ───────────────────────────────────────────────
        v3_settings = V3RegulatorSettings(
            max_output_w=max_output,
            min_output_w=min_discharge,
            kp_draw=kp_draw,
            kp_feed_in=kp_feed_in,
            control_interval_s=control_interval,
        )
        runtime.register_regulator(ZeroFeedV3Regulator(v3_settings))

        # ── Initial mode ──────────────────────────────────────────────────────
        mode_map = {
            "idle": DeviceMode.IDLE,
            "zero_feed": DeviceMode.DISCHARGE_ZERO_FEED,
        }
        await runtime.set_mode(mode_map.get(initial_mode, DeviceMode.IDLE))

        # ── MQTT / GridPythia integration ─────────────────────────────────────
        background_tasks: list[asyncio.Task] = []
        mqtt_client_ref = None

        if mqtt_broker:
            from clients.mqtt.client import MqttClient, MqttConfig
            from src.gridpythia.plan_subscriber import GridPythiaPlanSubscriber
            from src.gridpythia.status_reporter import GridPythiaStatusReporter

            mqtt_cfg = MqttConfig(
                broker=mqtt_broker,
                client_id=f"zerofeed-{device_id}",
                topic_prefix=topic_prefix,
            )
            mqtt_client = MqttClient(mqtt_cfg)
            mqtt_client_ref = mqtt_client

            plan_subscriber = GridPythiaPlanSubscriber(
                mqtt_client=mqtt_client,
                device_id=device_id,
                topic_prefix=topic_prefix,
            )
            plan_subscriber.register()
            mqtt_client.start()
            LOG.info("MQTT verbunden: %s (device_id=%s)", mqtt_broker, device_id)

            reporter = GridPythiaStatusReporter(
                mqtt_client=mqtt_client,
                battery=solarflow,
                device_id=device_id,
                topic_prefix=topic_prefix,
                interval_s=status_interval_s,
            )
            background_tasks.append(asyncio.create_task(reporter.run(), name="status-reporter"))

        # ── Start runtime ─────────────────────────────────────────────────────
        await runtime.start()
        LOG.info("ControlRuntime gestartet")

        # ── Start HTTP server ─────────────────────────────────────────────────
        app = create_app(runtime)
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)

        LOG.info("Dashboard erreichbar auf http://%s:%d", host, port)

        # Run uvicorn alongside the runtime
        uvicorn_task = asyncio.create_task(server.serve(), name="uvicorn")
        background_tasks.append(uvicorn_task)

        try:
            await asyncio.gather(*background_tasks)
        except asyncio.CancelledError:
            pass
        finally:
            LOG.info("Fahre Dashboard herunter …")
            server.should_exit = True
            for t in background_tasks:
                t.cancel()
            await asyncio.gather(*background_tasks, return_exceptions=True)
            await runtime.stop()
            if mqtt_client_ref is not None:
                mqtt_client_ref.stop()


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Zendure Dashboard Server – Steuerung + Live-Monitoring",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--shelly", default="192.168.178.77", metavar="IP")
    parser.add_argument("--zendure", default="192.168.178.140", metavar="IP")
    parser.add_argument("--host", default="0.0.0.0", metavar="HOST", help="Server-Bindungsadresse")
    parser.add_argument("--port", type=int, default=8765, metavar="PORT")
    parser.add_argument("--max-output", type=int, default=800, metavar="W")
    parser.add_argument("--min-discharge", type=int, default=20, metavar="W")
    parser.add_argument("--kp-draw", type=float, default=0.9, metavar="KP")
    parser.add_argument("--kp-feed-in", type=float, default=1.05, metavar="KP")
    parser.add_argument("--control-interval", type=float, default=3.0, metavar="S")
    parser.add_argument(
        "--initial-mode",
        choices=["idle", "zero_feed"],
        default="idle",
        help="Startmodus: idle (sicher) oder zero_feed",
    )
    parser.add_argument(
        "--mqtt-broker",
        default=None,
        metavar="URL",
        help="MQTT Broker URL (z.B. mqtt://192.168.1.5:1883)",
    )
    parser.add_argument("--device-id", default="SF800Pro", metavar="ID")
    parser.add_argument("--topic-prefix", default="gridpythia", metavar="PREFIX")
    parser.add_argument("--status-interval", type=float, default=60.0, metavar="S")
    parser.add_argument("--verbose", action="store_true")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    try:
        asyncio.run(
            run(
                shelly_ip=args.shelly,
                zendure_ip=args.zendure,
                host=args.host,
                port=args.port,
                max_output=args.max_output,
                min_discharge=args.min_discharge,
                kp_draw=args.kp_draw,
                kp_feed_in=args.kp_feed_in,
                control_interval=args.control_interval,
                mqtt_broker=args.mqtt_broker,
                device_id=args.device_id,
                topic_prefix=args.topic_prefix,
                status_interval_s=args.status_interval,
                initial_mode=args.initial_mode,
            )
        )
    except KeyboardInterrupt:
        LOG.info("Durch Benutzer beendet")


if __name__ == "__main__":
    main()

"""ZeroPythia Dashboard Server – main entry point.

Starts the dashboard server with real Shelly 3EM and Zendure SolarFlow.

Features:
  - WebSocket dashboard GUI at http://<host>:<port>/
  - Mode control: AC charge, idle, zero-feed discharge
  - Regulator selection and configuration via GUI
  - Live view: Shelly, battery, oscillation detection
  - Optional: GridPythia MQTT integration

Device limits and control parameters are configured in config/zerofeed.yaml.

Usage:
    python main.py
    python main.py --shelly 192.168.178.77 --zendure 192.168.178.140
    python main.py --port 8080 --host 0.0.0.0
    python main.py --mqtt-broker mqtt://192.168.1.5:1883 --device-id SF800Pro --auto
"""

import argparse
import asyncio
import logging
from pathlib import Path
from typing import Optional

import uvicorn

from clients.shelly.shelly import ShellyClient
from clients.zendure.http_client import SolarFlowAsyncClient
from ZeroPythia.config.zerofeed import load_config
from ZeroPythia.controller.zerofeed_regulator import ZeroFeedRegulator
from ZeroPythia.dashboard.server import create_app
from ZeroPythia.runtime.control_runtime import ControlRuntime
from ZeroPythia.runtime.models import DeviceMode

_CONFIG = Path("config") / "zerofeed.yaml"

LOG = logging.getLogger("main")


# ── Main ──────────────────────────────────────────────────────────────────────


async def run(
    shelly_ip: str,
    zendure_ip: str,
    host: str,
    port: int,
    mqtt_broker: Optional[str],
    device_id: str,
    topic_prefix: str,
    status_interval_s: float,
    initial_mode: str,
    auto_start: bool,
) -> None:
    # Load configuration to get control parameters
    yaml_cfg = load_config(_CONFIG)
    if yaml_cfg is None:
        raise RuntimeError(f"Failed to load configuration from {_CONFIG}")

    async with (
        ShellyClient(shelly_ip) as shelly_client,
        SolarFlowAsyncClient(zendure_ip) as solarflow,
    ):
        # ShellyClient directly implements GridMeterProtocol via get_phase_powers()
        # and get_total_power() – no adapter needed.
        runtime = ControlRuntime(
            grid_meter=shelly_client,
            battery=solarflow,
            sampling_interval_s=yaml_cfg.sampling_interval_s,
            control_interval_s=yaml_cfg.control_interval_s,
            max_discharge_w=yaml_cfg.max_output_w,
            min_discharge_w=yaml_cfg.min_output_w,
        )

        # ── Register regulators ───────────────────────────────────────────────

        # ── Regulator ──────────────────────────────────────────────────────
        runtime.register_regulator(ZeroFeedRegulator(settings=yaml_cfg, yaml_path=_CONFIG))

        # ── Initial mode ──────────────────────────────────────────────────────
        if mqtt_broker and auto_start:
            # Start in AUTO mode immediately via the new AutoModeManager
            await runtime.start()  # must be started before enable_auto_mode (creates tasks)
            await runtime.enable_auto_mode(
                mqtt_broker=mqtt_broker,
                device_id=device_id,
                topic_prefix=topic_prefix,
                status_interval_s=status_interval_s,
            )
            LOG.info("Auto mode started (device_id=%s, broker=%s)", device_id, mqtt_broker)
        else:
            mode_map = {
                "idle": DeviceMode.IDLE,
                "zero_feed": DeviceMode.DISCHARGE_ZERO_FEED,
            }
            await runtime.set_mode(mode_map.get(initial_mode, DeviceMode.IDLE))
            await runtime.start()

        LOG.info("ControlRuntime started")

        # ── Start HTTP server ─────────────────────────────────────────────────
        lang = yaml_cfg.language
        app = create_app(runtime, lang=lang)
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)

        LOG.info("Dashboard available at http://%s:%d", host, port)

        try:
            await server.serve()
        except asyncio.CancelledError:
            pass
        finally:
            LOG.info("Shutting down dashboard…")
            server.should_exit = True
            if runtime._auto_manager is not None:
                await runtime.disable_auto_mode()
            await runtime.stop()


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="ZeroPythia Dashboard Server – control + live monitoring",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--shelly", default="192.168.178.77", metavar="IP")
    parser.add_argument("--zendure", default="192.168.178.140", metavar="IP")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        metavar="HOST",
        help="Server bind address (e.g. 0.0.0.0 for LAN access)",
    )
    parser.add_argument("--port", type=int, default=8765, metavar="PORT")
    parser.add_argument(
        "--initial-mode",
        choices=["idle", "zero_feed"],
        default="idle",
        help="Initial mode: idle (safe) or zero_feed",
    )
    parser.add_argument(
        "--mqtt-broker",
        default="mqtt://localhost:1883",
        metavar="URL",
        help="MQTT broker URL. Use --auto to immediately enter AUTO mode.",
    )
    parser.add_argument("--device-id", default="SF800Pro", metavar="ID")
    parser.add_argument("--topic-prefix", default="gridpythia", metavar="PREFIX")
    parser.add_argument("--status-interval", type=float, default=60.0, metavar="S")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Start immediately in AUTO mode (uses --mqtt-broker + --device-id).",
    )
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
                mqtt_broker=args.mqtt_broker,
                device_id=args.device_id,
                topic_prefix=args.topic_prefix,
                status_interval_s=args.status_interval,
                initial_mode=args.initial_mode,
                auto_start=args.auto,
            )
        )
    except KeyboardInterrupt:
        LOG.info("Stopped by user")


if __name__ == "__main__":
    main()

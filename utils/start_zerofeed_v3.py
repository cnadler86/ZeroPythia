"""ZeroFeed V3 – Startscript für den echten Betrieb.

Startet den phasen-bewussten Zero-Feed Controller V3 mit
echtem Shelly 3EM und Zendure SolarFlow.

Loggt alle Messwerte und Controller-Interna als CSV (pro Tag):
    <log_dir>/zerofeed_v3_YYYY-MM-DD.csv

Zeilentypen:
    sample  – Shelly-Messwerte + Oszillationsdetektor-Zustand (~1s)
    control – Regler-Ausgaben + Setpoint (~1s)

Usage:
    python src/tools/start_zerofeed_v3.py
    python src/tools/start_zerofeed_v3.py --shelly 192.168.1.10 --zendure 192.168.1.20
    python src/tools/start_zerofeed_v3.py --log-dir /pfad/zu/logs --verbose
"""

import argparse
import asyncio
import logging
from pathlib import Path
from typing import Optional

from clients.shelly.shelly import ShellyClient
from clients.zendure.aiozen import SolarFlowAsyncClient
from src.controller.csv_logger import ZeroFeedCSVLogger
from src.controller.phase_controllers import (
    BatteryPhaseControllerSettings,
    DisturbanceControllerSettings,
    PhaseManagerSettings,
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
) -> None:
    settings = ZeroFeedV3Settings(
        manager=PhaseManagerSettings(
            max_output_w=max_output,
            min_output_w=min_discharge,
        ),
        battery_phase=BatteryPhaseControllerSettings(
            kp_draw=kp_draw,
            kp_feed_in=kp_feed_in,
            max_output_w=float(max_output),
        ),
        disturbance=DisturbanceControllerSettings(
            max_compensation_w=float(max_output),
        ),
        control_interval_s=control_interval,
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
        await controller.start()

        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            LOG.info("Fahre ZeroFeed V3 herunter …")
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
            )
        )
    except KeyboardInterrupt:
        LOG.info("Durch Benutzer beendet")


if __name__ == "__main__":
    main()

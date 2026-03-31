"""ZeroFeed V3 – Vollständiger Pipeline-Lauf.
==========================================

1. CSV-Daten bereinigen
2. Batch-Simulation mit Standardparametern
3. Parameter-Optimierung
4. Erneute Simulation mit optimierten Parametern

Verwendung:
    python -m src.tools.run_pipeline
"""

import asyncio
import logging
import sys
from pathlib import Path

from simulator.batch_runner import BatchRunner
from simulator.optimizer import ParameterOptimizer
from src.controller.oscillation_detectorv2 import (
    BaseloadHolderSettings,
    BaseloadPredictorSettings,
)
from src.controller.phase_controllers import (
    BatteryPhaseControllerSettings,
    DisturbanceControllerSettings,
    PhaseManagerSettings,
)
from src.controller.zerofeed_v3 import ZeroFeedV3Settings
from utils.clean_csv import clean_all_csvs

logger = logging.getLogger(__name__)

CSV_DIR = Path(__file__).parent.parent.parent.parent / "shelly_logger" / "ShellyLog"


def get_default_settings() -> ZeroFeedV3Settings:
    return ZeroFeedV3Settings(
        manager=PhaseManagerSettings(
            min_output_w=20,
            max_output_w=800,
            target_total_grid_w=5.0,
            min_change_w=3.0,
        ),
        disturbance=DisturbanceControllerSettings(
            kp=1.0,
            hysteresis_w=5.0,
            kp_hysteresis=0.3,
        ),
        battery_phase=BatteryPhaseControllerSettings(
            kp_draw=0.95,
            kp_feed_in=1.05,
            hysteresis_w=10.0,
            kp_hysteresis=0.3,
            target_power_w=5.0,
        ),
        holder_settings=BaseloadHolderSettings(),
        predictor_settings=BaseloadPredictorSettings(),
        sampling_interval=1.0,
        control_interval_s=3.0,
    )


async def run_pipeline(skip_clean: bool = False, skip_optimize: bool = False):
    if not CSV_DIR.exists():
        print(f"CSV-Verzeichnis nicht gefunden: {CSV_DIR}")
        sys.exit(1)

    # 1. CSV bereinigen
    if not skip_clean:
        print("=" * 80)
        print("SCHRITT 1: CSV-Daten bereinigen")
        print("=" * 80)
        clean_all_csvs(CSV_DIR, overwrite=True)
        print()

    # 2. Baseline-Simulation
    print("=" * 80)
    print("SCHRITT 2: Baseline-Simulation mit Standardparametern")
    print("=" * 80)

    settings = get_default_settings()
    runner = BatchRunner(
        csv_dir=CSV_DIR,
        settings=settings,
        three_phase_only=True,
    )

    baseline_results = await runner.run_all(show_progress=True)
    print()
    BatchRunner.print_summary(baseline_results)

    # 3. Optimierung
    if not skip_optimize:
        print()
        print("=" * 80)
        print("SCHRITT 3: Parameter-Optimierung")
        print("=" * 80)

        optimizer = ParameterOptimizer(
            csv_dir=CSV_DIR,
            base_settings=settings,
            three_phase_only=True,
        )

        opt_results = await optimizer.optimize_phase_controller()
        ParameterOptimizer.print_results(opt_results, top_n=10)

        if opt_results:
            best = opt_results[0]
            print(f"\nBeste Parameter: {best.label}")
            print(f"Score: {best.score:.1f}")

            # 4. Simulation mit optimierten Parametern
            print()
            print("=" * 80)
            print("SCHRITT 4: Simulation mit optimierten Parametern")
            print("=" * 80)

            opt_runner = BatchRunner(
                csv_dir=CSV_DIR,
                settings=best.settings,
                three_phase_only=True,
            )

            opt_sim_results = await opt_runner.run_all(show_progress=True)
            print()
            BatchRunner.print_summary(opt_sim_results)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s - %(message)s",
    )

    skip_clean = "--skip-clean" in sys.argv
    skip_optimize = "--skip-optimize" in sys.argv

    asyncio.run(run_pipeline(skip_clean=skip_clean, skip_optimize=skip_optimize))

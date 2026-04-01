"""Quick test: batch simulate + optimize ZeroFeed V3."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from simulator.batch_runner import BatchRunner
from simulator.optimizer import ParameterOptimizer
from src.controller.oscillation_detectorv2 import (
    BaseloadHolderSettings,
    BaseloadPredictorSettings,
)
from src.controller.phase_controller import (
    InverterPhaseControllerSettings,
    PhaseControllerSettings,
    ZeroFeedManagerSettings,
)
from src.controller.zerofeed_v3 import ZeroFeedV3Settings

CSV_DIR = Path(__file__).parent.parent.parent.parent / "shelly_logger" / "ShellyLog"


def get_settings() -> ZeroFeedV3Settings:
    return ZeroFeedV3Settings(
        manager=ZeroFeedManagerSettings(min_output_w=20, max_output_w=800),
        phase_controller=PhaseControllerSettings(kp=1.0, hysteresis_w=5.0),
        inverter_controller=InverterPhaseControllerSettings(
            kp_draw=0.95, kp_feed_in=1.05, hysteresis_w=10.0, target_power_w=5.0
        ),
        holder_settings=BaseloadHolderSettings(),
        predictor_settings=BaseloadPredictorSettings(),
        sampling_interval=1.0,
        control_interval_s=3.0,
    )


async def main():
    settings = get_settings()

    # Batch simulation
    print("=" * 80)
    print("BATCH SIMULATION")
    print("=" * 80)
    runner = BatchRunner(csv_dir=CSV_DIR, settings=settings, three_phase_only=True)
    results = await runner.run_all(show_progress=True)
    BatchRunner.print_summary(results)

    # Optimization
    print("\n" + "=" * 80)
    print("OPTIMIERUNG")
    print("=" * 80)
    optimizer = ParameterOptimizer(csv_dir=CSV_DIR, base_settings=settings, three_phase_only=True)
    opt_results = await optimizer.optimize_phase_controller()
    ParameterOptimizer.print_results(opt_results, top_n=10)

    if opt_results:
        best = opt_results[0]
        print(f"\nBeste Parameter: {best.label}")

        # Run with optimized parameters
        print("\n" + "=" * 80)
        print("MIT OPTIMIERTEN PARAMETERN")
        print("=" * 80)
        opt_runner = BatchRunner(csv_dir=CSV_DIR, settings=best.settings, three_phase_only=True)
        opt_sim = await opt_runner.run_all(show_progress=True)
        BatchRunner.print_summary(opt_sim)


if __name__ == "__main__":
    asyncio.run(main())

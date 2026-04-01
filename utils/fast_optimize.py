"""Fast optimization: reduced parameter grid for quick results."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import itertools

from tqdm import tqdm

from simulator.batch_runner import BatchRunner, compute_statistics, run_simulation
from simulator.grid_simulator import clean_csv_data, load_csv
from simulator.optimizer import compute_score
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


def make_settings(kp_draw, kp_feed_in, dist_kp, hyst) -> ZeroFeedV3Settings:
    return ZeroFeedV3Settings(
        manager=ZeroFeedManagerSettings(min_output_w=20, max_output_w=800),
        phase_controller=PhaseControllerSettings(kp=dist_kp, hysteresis_w=hyst, kp_hysteresis=0.3),
        inverter_controller=InverterPhaseControllerSettings(
            kp_draw=kp_draw,
            kp_feed_in=kp_feed_in,
            hysteresis_w=hyst,
            kp_hysteresis=0.3,
            target_power_w=5.0,
        ),
        holder_settings=BaseloadHolderSettings(),
        predictor_settings=BaseloadPredictorSettings(),
        sampling_interval=1.0,
        control_interval_s=3.0,
    )


async def main():
    # Load all 3-phase CSVs once
    runner = BatchRunner(
        csv_dir=CSV_DIR, settings=make_settings(0.95, 1.05, 1.0, 10.0), three_phase_only=True
    )
    csv_files = runner.find_csv_files()

    all_records = {}
    for f in csv_files:
        records = load_csv(f)
        records = clean_csv_data(records)
        if records:
            all_records[f.name] = records

    print(f"Geladen: {len(all_records)} CSV-Dateien\n")

    # Reduced parameter grid
    kp_draws = [0.8, 0.9, 0.95, 1.0]
    kp_feedins = [1.0, 1.05, 1.1]
    dist_kps = [0.9, 1.0]
    hystereses = [5.0, 10.0]

    configs = list(itertools.product(kp_draws, kp_feedins, dist_kps, hystereses))
    print(f"Teste {len(configs)} Konfigurationen × {len(all_records)} CSVs\n")

    results = []
    for kp_d, kp_fi, d_kp, hyst in tqdm(configs, desc="Optimierung"):
        settings = make_settings(kp_d, kp_fi, d_kp, hyst)
        stats_list = []

        for csv_name, records in all_records.items():
            result = await run_simulation(
                records=records,
                settings=settings,
                csv_name=csv_name,
                show_progress=False,
            )
            stats = compute_statistics(result)
            stats_list.append(stats)

        score = compute_score(stats_list)
        label = f"kp_d={kp_d} kp_fi={kp_fi} d_kp={d_kp} hyst={hyst}"
        mean_eff = sum(s.efficiency_pct for s in stats_list) / len(stats_list)
        mean_band = sum(s.time_in_band_pct for s in stats_list) / len(stats_list)
        total_export = sum(s.total_grid_export_wh for s in stats_list)
        results.append((score, label, mean_eff, mean_band, total_export, settings))

    # Sort by score
    results.sort(key=lambda r: r[0], reverse=True)

    print("\n" + "=" * 100)
    print(f"{'#':>3} {'Score':>7} {'Eff%':>6} {'Band%':>6} {'Export':>8} Parameter")
    print("-" * 100)
    for i, (score, label, eff, band, export, _) in enumerate(results[:15]):
        print(f"{i + 1:>3} {score:>6.1f} {eff:>5.1f}% {band:>5.1f}% {export:>7.0f} {label}")
    print("=" * 100)

    # Final run with best params
    if results:
        best_settings = results[0][5]
        print(f"\nBeste Konfiguration: {results[0][1]}")
        print("\nSimulation mit optimierten Parametern:")
        opt_runner = BatchRunner(csv_dir=CSV_DIR, settings=best_settings, three_phase_only=True)
        opt_results = await opt_runner.run_all(show_progress=True)
        BatchRunner.print_summary(opt_results)


if __name__ == "__main__":
    asyncio.run(main())

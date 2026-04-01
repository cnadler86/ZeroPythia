"""Parameter-Optimierer für ZeroFeed V3.
======================================

Grid-Search Optimierung über Controller-Parameter.
Ziel: Batterie-Energie möglichst gut nutzen bei minimalem Netzbezug.

Optimierungsziel:
  - Minimiere Netzbezug (grid_import)
  - Minimiere Einspeisung (grid_export) – verschenkte Energie
  - Maximiere Zeit im Zielband
  - Score = efficiency * band_pct - penalty * export
"""

import itertools
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from tqdm import tqdm

from src.controller.phase_controller import (
    InverterPhaseControllerSettings,
    PhaseControllerSettings,
)
from src.controller.zerofeed_v3 import ZeroFeedV3Settings

from .batch_runner import (
    BatchRunner,
    Statistics,
    compute_statistics,
    run_simulation,
)
from .grid_simulator import PhaseRecord, clean_csv_data, load_csv

logger = logging.getLogger(__name__)


@dataclass
class OptimizationResult:
    """Ergebnis einer Parametervariation."""

    label: str
    settings: ZeroFeedV3Settings
    score: float
    stats_list: List[Statistics] = field(default_factory=list)

    @property
    def mean_efficiency(self) -> float:
        if not self.stats_list:
            return 0
        return sum(s.efficiency_pct for s in self.stats_list) / len(self.stats_list)

    @property
    def mean_band_pct(self) -> float:
        if not self.stats_list:
            return 0
        return sum(s.time_in_band_pct for s in self.stats_list) / len(self.stats_list)

    @property
    def total_export_wh(self) -> float:
        return sum(s.total_grid_export_wh for s in self.stats_list)


def compute_score(stats_list: List[Statistics]) -> float:
    """Berechnet Score einer Parameterkonfiguration.

    Score = mittlere Effizienz × mittleres Band% - Export-Strafe

    Höher = besser:
    - Effizienz: wie viel der Batterie den Bezug reduziert (0-100%)
    - Band: wie oft wir im Zielband sind (0-100%)
    - Export-Strafe: verschenkte Energie ist schlecht
    """
    if not stats_list:
        return 0.0

    mean_eff = sum(s.efficiency_pct for s in stats_list) / len(stats_list)
    mean_band = sum(s.time_in_band_pct for s in stats_list) / len(stats_list)
    total_export = sum(s.total_grid_export_wh for s in stats_list)
    total_battery = sum(s.total_battery_wh for s in stats_list)

    # Normalisiere Export relativ zur Batterie-Nutzung
    export_ratio = total_export / max(total_battery, 1) * 100

    # Score: Effizienz und Band-Anteil belohnen, Export bestrafen
    score = mean_eff * 0.6 + mean_band * 0.4 - export_ratio * 0.5

    return score


class ParameterOptimizer:
    """Grid-Search Optimierung über Controller-Parameter.

    Variiert systematisch die wichtigsten Parameter und
    evaluiert jede Konfiguration über alle CSV-Dateien.
    """

    def __init__(
        self,
        csv_dir: Path,
        base_settings: ZeroFeedV3Settings,
        three_phase_only: bool = True,
        battery_capacity_wh: int = 100_000,
    ):
        self.csv_dir = csv_dir
        self.base_settings = base_settings
        self.three_phase_only = three_phase_only
        self.battery_capacity_wh = battery_capacity_wh
        self._records_cache: Dict[str, List[PhaseRecord]] = {}

    def _load_all_csvs(self) -> Dict[str, List[PhaseRecord]]:
        """Lädt alle CSVs einmal (gecacht)."""
        if self._records_cache:
            return self._records_cache

        runner = BatchRunner(
            csv_dir=self.csv_dir,
            settings=self.base_settings,
            three_phase_only=self.three_phase_only,
        )
        files = runner.find_csv_files()

        for f in files:
            records = load_csv(f)
            records = clean_csv_data(records)
            if records:
                self._records_cache[f.name] = records

        return self._records_cache

    async def _evaluate_settings(
        self,
        settings: ZeroFeedV3Settings,
        label: str,
    ) -> OptimizationResult:
        """Evaluiert eine Konfiguration über alle CSV-Dateien."""
        all_records = self._load_all_csvs()
        stats_list: List[Statistics] = []

        for csv_name, records in all_records.items():
            result = await run_simulation(
                records=records,
                settings=settings,
                csv_name=csv_name,
                battery_capacity_wh=self.battery_capacity_wh,
                show_progress=False,
            )
            stats = compute_statistics(result)
            stats_list.append(stats)

        score = compute_score(stats_list)
        return OptimizationResult(
            label=label,
            settings=settings,
            score=score,
            stats_list=stats_list,
        )

    async def optimize_phase_controller(self) -> List[OptimizationResult]:
        """Optimiert die Phase-Controller Parameter.

        Variiert:
        - kp_draw: Verstärkung bei Netzbezug (vorsichtig)
        - kp_feed_in: Verstärkung bei Einspeisung (aggressiv)
        - disturbance kp: Kompensation der Fremdphasen
        - hysteresis: Totzone
        """
        kp_draws = [0.7, 0.8, 0.9, 0.95, 1.0]
        kp_feedins = [1.0, 1.05, 1.1, 1.2]
        dist_kps = [0.9, 1.0]
        hystereses = [5.0, 10.0, 15.0]

        configs = list(itertools.product(kp_draws, kp_feedins, dist_kps, hystereses))
        logger.info(
            "Optimierung: %d Konfigurationen × %d CSVs",
            len(configs),
            len(self._load_all_csvs()),
        )

        results: List[OptimizationResult] = []

        for kp_d, kp_fi, d_kp, hyst in tqdm(configs, desc="Optimierung"):
            label = f"kp_d={kp_d} kp_fi={kp_fi} d_kp={d_kp} hyst={hyst}"

            settings = ZeroFeedV3Settings(
                manager=self.base_settings.manager,
                phase_controller=PhaseControllerSettings(
                    kp=d_kp,
                    hysteresis_w=hyst,
                    kp_hysteresis=0.3,
                ),
                inverter_controller=InverterPhaseControllerSettings(
                    kp_draw=kp_d,
                    kp_feed_in=kp_fi,
                    hysteresis_w=hyst,
                    kp_hysteresis=0.3,
                    target_power_w=self.base_settings.inverter_controller.target_power_w,
                ),
                holder_settings=self.base_settings.holder_settings,
                predictor_settings=self.base_settings.predictor_settings,
                sampling_interval=self.base_settings.sampling_interval,
                control_interval_s=self.base_settings.control_interval_s,
            )

            opt_result = await self._evaluate_settings(settings, label)
            results.append(opt_result)

        # Sortiere nach Score (höchster zuerst)
        results.sort(key=lambda r: r.score, reverse=True)
        return results

    @staticmethod
    def print_results(results: List[OptimizationResult], top_n: int = 10) -> None:
        """Druckt Top-N Ergebnisse."""
        print("\n" + "=" * 100)
        print(f"Top {min(top_n, len(results))} Parameterkonfigurationen")
        print("-" * 100)
        print(f"{'#':>3} {'Score':>7} {'Eff%':>6} {'Band%':>6} {'Export':>8} {'Parameter'}")
        print("-" * 100)

        for i, r in enumerate(results[:top_n]):
            print(
                f"{i + 1:>3} "
                f"{r.score:>6.1f} "
                f"{r.mean_efficiency:>5.1f}% "
                f"{r.mean_band_pct:>5.1f}% "
                f"{r.total_export_wh:>7.0f} "
                f"{r.label}"
            )

        print("=" * 100)

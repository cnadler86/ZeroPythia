"""Batch Runner – Simulation über alle CSV-Dateien.

=================================================

Führt den ZeroFeed V3 Controller mit Grid Simulator durch und
sammelt Statistiken für Parameter-Optimierung.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple, cast

from tqdm import tqdm

from clients.zendure.mock.async_mock_client import SolarFlowAsyncMockClient
from src.controller.zerofeed_v3 import (
    BatteryInverter,
    GridSample,
    ZeroFeedV3Controller,
    ZeroFeedV3Settings,
)

from .grid_simulator import GridSimulator, PhaseRecord, clean_csv_data, load_csv

logger = logging.getLogger(__name__)


@dataclass
class SimulationResult:
    """Ergebnis einer einzelnen Simulation."""

    csv_file: str
    duration_s: float
    num_samples: int

    # Zeitreihen
    timestamps: List[float] = field(default_factory=list)
    grid_total: List[float] = field(default_factory=list)
    phase_a: List[float] = field(default_factory=list)
    phase_b: List[float] = field(default_factory=list)
    phase_c: List[float] = field(default_factory=list)
    battery_output: List[float] = field(default_factory=list)
    setpoints: List[float] = field(default_factory=list)
    osc_limits: List[float] = field(default_factory=list)
    phase_a_correction: List[float] = field(default_factory=list)
    phase_b_correction: List[float] = field(default_factory=list)
    phase_c_correction: List[float] = field(default_factory=list)
    phase_b_desired_total: List[float] = field(default_factory=list)
    phase_a_osc_limit: List[float] = field(default_factory=list)
    phase_b_osc_limit: List[float] = field(default_factory=list)
    phase_c_osc_limit: List[float] = field(default_factory=list)


@dataclass
class Statistics:
    """Statistiken einer Simulation."""

    csv_file: str
    duration_h: float
    num_samples: int

    # Energie
    total_grid_import_wh: float = 0.0  # Netzbezug
    total_grid_export_wh: float = 0.0  # Einspeisung
    total_battery_wh: float = 0.0  # Batterie-Entladung
    efficiency_pct: float = 0.0  # Batterie-zu-Bezugsreduktion

    # Regelgüte
    mean_grid_w: float = 0.0
    std_grid_w: float = 0.0
    max_grid_w: float = 0.0
    min_grid_w: float = 0.0
    time_in_band_pct: float = 0.0  # % der Zeit im Zielband

    # Oszillation
    oscillation_time_pct: float = 0.0


def compute_statistics(
    result: SimulationResult, target_w: float = 5.0, band_w: float = 20.0
) -> Statistics:
    """Berechnet Statistiken aus Simulationsergebnis."""
    if not result.timestamps or len(result.timestamps) < 2:
        return Statistics(csv_file=result.csv_file, duration_h=0, num_samples=0)

    duration_h = result.duration_s / 3600.0
    n = len(result.grid_total)

    # Zeitschritte für Energieberechnung
    dt_h_list = []
    for i in range(1, len(result.timestamps)):
        dt_h_list.append((result.timestamps[i] - result.timestamps[i - 1]) / 3600.0)

    # Energie
    grid_import = 0.0
    grid_export = 0.0
    battery_total = 0.0

    for i, dt_h in enumerate(dt_h_list):
        idx = i + 1
        if idx < len(result.grid_total):
            p = result.grid_total[idx]
            if p > 0:
                grid_import += p * dt_h
            else:
                grid_export += abs(p) * dt_h

        if idx < len(result.battery_output):
            battery_total += result.battery_output[idx] * dt_h

    # Effizienz: wie viel Batterie-Energie den Bezug reduziert hat
    efficiency = (battery_total / max(battery_total + grid_import, 1)) * 100

    # Statistiken
    import statistics as stat_mod

    grid_vals = result.grid_total
    mean_grid = stat_mod.mean(grid_vals) if grid_vals else 0
    std_grid = stat_mod.stdev(grid_vals) if len(grid_vals) > 1 else 0
    max_grid = max(grid_vals) if grid_vals else 0
    min_grid = min(grid_vals) if grid_vals else 0

    # Zeit im Zielband
    in_band = sum(1 for v in grid_vals if abs(v - target_w) <= band_w)
    time_in_band = (in_band / max(n, 1)) * 100

    # Oszillationszeit
    osc_count = sum(1 for v in result.osc_limits if v < 800)
    osc_pct = (osc_count / max(n, 1)) * 100

    return Statistics(
        csv_file=result.csv_file,
        duration_h=duration_h,
        num_samples=n,
        total_grid_import_wh=grid_import,
        total_grid_export_wh=grid_export,
        total_battery_wh=battery_total,
        efficiency_pct=efficiency,
        mean_grid_w=mean_grid,
        std_grid_w=std_grid,
        max_grid_w=max_grid,
        min_grid_w=min_grid,
        time_in_band_pct=time_in_band,
        oscillation_time_pct=osc_pct,
    )


async def run_simulation(
    records: List[PhaseRecord],
    settings: ZeroFeedV3Settings,
    csv_name: str = "",
    initial_soc: int = 100,
    battery_capacity_wh: int = 100_000,  # Sehr groß für unbegrenzte Energie
    show_progress: bool = True,
) -> SimulationResult:
    """Führt eine Simulation mit dem ZeroFeed V3 Controller durch.

    Args:
        records: Bereinigte CSV-Daten
        settings: Controller-Einstellungen
        csv_name: Name der CSV-Datei (für Logs)
        initial_soc: Start-SOC (100 = voll)
        battery_capacity_wh: Batteriekapazität (sehr groß = unbegrenzt)
        show_progress: tqdm Fortschrittsanzeige

    Returns:
        SimulationResult mit Zeitreihen
    """
    if not records:
        return SimulationResult(csv_file=csv_name, duration_s=0, num_samples=0)

    # Mock-Batterie erstellen
    battery = SolarFlowAsyncMockClient(
        initial_soc=initial_soc,
        battery_capacity_wh=battery_capacity_wh,
    )

    # Grid Simulator erstellen
    grid_sim = GridSimulator(records=records, battery_mock=battery)

    # Controller erstellen
    controller = ZeroFeedV3Controller(
        settings=settings,
        grid_meter=grid_sim,
        battery=cast(BatteryInverter, battery),
    )

    # Ergebnis-Listen
    result = SimulationResult(
        csv_file=csv_name,
        duration_s=grid_sim.duration_s,
        num_samples=len(records),
    )

    # Simulation: Batterie starten
    sim_time = grid_sim.start_time
    battery.set_simulation_time(sim_time)
    grid_sim.set_simulation_time(sim_time)

    min_output = settings.manager.min_output_w
    await battery.start_discharge(min_output)

    # Warte bis Batterie bereit (simuliert)
    for _ in range(20):
        sim_time += 0.5
        battery.set_simulation_time(sim_time)
        power = await battery.get_ac_output_power()
        if power is not None and power >= min_output:
            break

    controller._current_output_limit = min_output

    # Hauptschleife: Sample für Sample durchgehen
    control_interval = settings.control_interval_s
    last_control_time = sim_time

    iterator = tqdm(records, desc=csv_name, disable=not show_progress, leave=False)

    for rec in iterator:
        sim_time = rec.timestamp
        battery.set_simulation_time(sim_time)
        grid_sim.set_simulation_time(sim_time)

        # Phasen lesen (mit Batterie-Kopplung auf Phase B)
        phases = await grid_sim.get_phase_powers()
        if phases is None:
            continue

        phase_a, phase_b, phase_c = phases
        output_power = await battery.get_ac_output_power() or 0

        sample = GridSample(
            timestamp=sim_time,
            phase_a=phase_a,
            phase_b=phase_b,
            phase_c=phase_c,
            battery_output=float(output_power),
        )

        # Oszillationserkennung (mit Sampling-Rate)
        await controller.add_sample(sample)

        # Regelung: regulärer Takt + schneller Schutz bei drohender Überkompensation
        if (
            controller.needs_fast_recontrol(sample)
            or sim_time - last_control_time >= control_interval
        ):
            await controller.perform_control()
            last_control_time = sim_time

        # Daten aufzeichnen
        result.timestamps.append(sim_time)
        result.grid_total.append(phase_a + phase_b + phase_c)
        result.phase_a.append(phase_a)
        result.phase_b.append(phase_b)
        result.phase_c.append(phase_c)
        result.battery_output.append(float(output_power))
        result.setpoints.append(float(controller.current_output_limit))
        result.phase_a_correction.append(controller.manager._phase_a.last_output)
        result.phase_b_correction.append(controller.manager._phase_b.last_output)
        result.phase_c_correction.append(controller.manager._phase_c.last_output)
        result.phase_b_desired_total.append(controller.manager._phase_b.last_desired_total)
        result.phase_a_osc_limit.append(controller.manager._phase_a.last_osc_limit)
        result.phase_b_osc_limit.append(controller.manager._phase_b.last_osc_limit)
        result.phase_c_osc_limit.append(controller.manager._phase_c.last_osc_limit)

        # Oszillations-Limit
        osc_lim = float(settings.manager.max_output_w)
        for ctrl in controller.manager.phases.values():
            if ctrl.is_oscillating:
                osc_lim = min(osc_lim, ctrl.get_osc_limit())
        if controller.manager.total_is_oscillating:
            osc_lim = min(osc_lim, controller.manager.get_total_osc_limit())
        result.osc_limits.append(osc_lim)

    return result


class BatchRunner:
    """Führt Simulationen über alle CSV-Dateien in einem Verzeichnis durch."""

    def __init__(
        self,
        csv_dir: Path,
        settings: ZeroFeedV3Settings,
        three_phase_only: bool = True,
        initial_soc: int = 100,
        battery_capacity_wh: int = 100_000,
    ):
        self.csv_dir = csv_dir
        self.settings = settings
        self.three_phase_only = three_phase_only
        self.initial_soc = initial_soc
        self.battery_capacity_wh = battery_capacity_wh

    def find_csv_files(self) -> List[Path]:
        """Findet alle CSV-Dateien im Verzeichnis."""
        files = sorted(self.csv_dir.glob("shelly3em_power_*.csv"))

        if self.three_phase_only:
            # Nur 3-Phasen-Dateien (ab 2026-02-22)
            filtered = []
            for f in files:
                with open(f, "r", encoding="utf-8") as fh:
                    header = fh.readline()
                    if "Phase A" in header:
                        filtered.append(f)
            files = filtered

        return files

    async def run_all(
        self, show_progress: bool = True
    ) -> List[Tuple[SimulationResult, Statistics]]:
        """Führt alle Simulationen durch."""
        files = self.find_csv_files()
        logger.info("Gefunden: %d CSV-Dateien", len(files))

        results: List[Tuple[SimulationResult, Statistics]] = []

        for csv_file in tqdm(files, desc="CSV-Dateien", disable=not show_progress):
            records = load_csv(csv_file)
            records = clean_csv_data(records)

            if not records:
                logger.warning("Keine Daten in %s", csv_file.name)
                continue

            result = await run_simulation(
                records=records,
                settings=self.settings,
                csv_name=csv_file.name,
                initial_soc=self.initial_soc,
                battery_capacity_wh=self.battery_capacity_wh,
                show_progress=show_progress,
            )

            stats = compute_statistics(result)
            results.append((result, stats))

            if show_progress:
                tqdm.write(
                    f"  {csv_file.name}: "
                    f"mean={stats.mean_grid_w:+.1f}W  "
                    f"std={stats.std_grid_w:.1f}W  "
                    f"band={stats.time_in_band_pct:.0f}%  "
                    f"eff={stats.efficiency_pct:.0f}%  "
                    f"osc={stats.oscillation_time_pct:.0f}%"
                )

        return results

    @staticmethod
    def print_summary(results: List[Tuple[SimulationResult, Statistics]]) -> None:
        """Druckt Zusammenfassung aller Simulationen."""
        if not results:
            print("Keine Ergebnisse.")
            return

        print("\n" + "=" * 90)
        print(
            f"{'Datei':<35} {'Mean':>8} {'Std':>8} {'Band%':>6} {'Eff%':>6} {'Osc%':>6} {'Import':>10} {'Export':>10}"
        )
        print("-" * 90)

        total_import = 0.0
        total_export = 0.0
        total_battery = 0.0

        for _result, stats in results:
            print(
                f"{stats.csv_file:<35} "
                f"{stats.mean_grid_w:>+7.1f} "
                f"{stats.std_grid_w:>7.1f} "
                f"{stats.time_in_band_pct:>5.0f}% "
                f"{stats.efficiency_pct:>5.0f}% "
                f"{stats.oscillation_time_pct:>5.0f}% "
                f"{stats.total_grid_import_wh:>9.0f} "
                f"{stats.total_grid_export_wh:>9.0f}"
            )
            total_import += stats.total_grid_import_wh
            total_export += stats.total_grid_export_wh
            total_battery += stats.total_battery_wh

        print("-" * 90)
        total_eff = (total_battery / max(total_battery + total_import, 1)) * 100
        print(
            f"{'GESAMT':<35} {'':>8} {'':>8} {'':>6} {total_eff:>5.0f}% {'':>6} "
            f"{total_import:>9.0f} {total_export:>9.0f}"
        )
        print(f"  Batterie-Entladung: {total_battery:.0f} Wh")
        print("=" * 90)

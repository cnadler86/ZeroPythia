"""Batch Runner – V4 Simulation über CSV-Dateien.

Treibt ``_V4Core`` direkt mit simulierten CSV-Zeitstempeln.
Das PT1-Batteriemodell verwendet die CSV-Timestamps, nicht die echte Systemuhr –
die Simulation läuft daher in beliebiger Geschwindigkeit.

Kein asyncio nötig: ``_V4Core.calculate()`` ist synchron.
"""

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from src.config.zerofeed_v4 import ZeroFeedV4Config
from src.controller.phase_controller import PhaseSample
from src.dashboard.regulators.v4_adapter import _V4Core

from .grid_simulator import PhaseRecord, clean_csv_data, load_csv

logger = logging.getLogger(__name__)


# ── PT1 Hilfe ─────────────────────────────────────────────────────────────────


def _pt1(
    sp_sent_at: float,
    sp_target_w: float,
    sp_prev_w: float,
    dead_time: float,
    tau: float,
    t: float,
) -> float:
    """PT1-Schätzung der Batterie-AC-Leistung zum simulierten Zeitpunkt t."""
    if sp_sent_at == 0.0:
        return 0.0
    elapsed = t - (sp_sent_at + dead_time)
    if elapsed <= 0.0:
        return sp_prev_w
    factor = 1.0 - math.exp(-elapsed / tau) if tau > 0 else 1.0
    return sp_prev_w + (sp_target_w - sp_prev_w) * factor


# ── Ergebnismodelle ───────────────────────────────────────────────────────────


@dataclass
class SimulationResult:
    """Zeitreihen-Daten einer V4 Simulation."""

    csv_file: str
    duration_s: float
    num_samples: int
    control_phase: str = "B"

    # Zeitstempel
    timestamps: List[float] = field(default_factory=list)

    # Netz-Messwerte (control_phase hat Batterie-Output bereits abgezogen)
    phase_a: List[float] = field(default_factory=list)
    phase_b: List[float] = field(default_factory=list)
    phase_c: List[float] = field(default_factory=list)
    grid_total: List[float] = field(default_factory=list)

    # Batterie
    battery_output: List[float] = field(default_factory=list)  # PT1-Schätzung
    setpoints: List[float] = field(default_factory=list)

    # Controller-Ausgaben (gehalten zwischen Control-Zyklen)
    # ff_per_phase[X] = FF-Anfrage für Phase X (0 wenn X = control_phase)
    ff_a: List[float] = field(default_factory=list)
    ff_b: List[float] = field(default_factory=list)
    ff_c: List[float] = field(default_factory=list)
    fb_correction: List[float] = field(default_factory=list)
    ff_sum: List[float] = field(default_factory=list)

    # Oszillationszustand pro Phase
    osc_active_a: List[bool] = field(default_factory=list)
    osc_active_b: List[bool] = field(default_factory=list)
    osc_active_c: List[bool] = field(default_factory=list)

    def correction(self, phase: str) -> List[float]:
        """Gibt die Regler-Ausgabe für eine Phase zurück.

        FF-Phase  → FF-Anfrage (positiv = Batterie fordert mehr)
        FB-Phase  → FB-Korrektur
        """
        if phase == "A":
            return self.ff_a if phase != self.control_phase else self.fb_correction
        if phase == "B":
            return self.ff_b if phase != self.control_phase else self.fb_correction
        return self.ff_c if phase != self.control_phase else self.fb_correction

    def phase_values(self, phase: str) -> List[float]:
        if phase == "A":
            return self.phase_a
        if phase == "B":
            return self.phase_b
        return self.phase_c

    def osc_active(self, phase: str) -> List[bool]:
        if phase == "A":
            return self.osc_active_a
        if phase == "B":
            return self.osc_active_b
        return self.osc_active_c


@dataclass
class Statistics:
    """Zusammenfassung einer Simulation."""

    csv_file: str
    duration_h: float
    num_samples: int
    mean_grid_w: float
    std_grid_w: float
    min_grid_w: float
    max_grid_w: float
    time_in_band_pct: float
    total_grid_import_wh: float
    total_grid_export_wh: float
    total_battery_wh: float
    efficiency_pct: float
    oscillation_time_pct: float


# ── Statistik ─────────────────────────────────────────────────────────────────


def compute_statistics(result: SimulationResult, target_power_w: float = 3.0) -> Statistics:
    """Berechnet Zusammenfassungs-Statistik aus einem SimulationResult."""
    if not result.timestamps:
        return Statistics(
            csv_file=result.csv_file,
            duration_h=0.0,
            num_samples=0,
            mean_grid_w=0.0,
            std_grid_w=0.0,
            min_grid_w=0.0,
            max_grid_w=0.0,
            time_in_band_pct=0.0,
            total_grid_import_wh=0.0,
            total_grid_export_wh=0.0,
            total_battery_wh=0.0,
            efficiency_pct=0.0,
            oscillation_time_pct=0.0,
        )

    grid = result.grid_total
    n = len(grid)
    mean_w = sum(grid) / n
    std_w = math.sqrt(sum((g - mean_w) ** 2 for g in grid) / n)

    band_w = 20.0
    in_band = sum(1 for g in grid if abs(g - target_power_w) <= band_w)
    time_in_band_pct = 100.0 * in_band / n

    cum_import = cum_export = cum_battery = 0.0
    for i in range(1, n):
        dt_h = (result.timestamps[i] - result.timestamps[i - 1]) / 3600.0
        p = grid[i]
        if p > 0:
            cum_import += p * dt_h
        else:
            cum_export += abs(p) * dt_h
        cum_battery += result.battery_output[i] * dt_h

    efficiency_pct = (
        100.0 * cum_battery / (cum_import + cum_battery) if (cum_import + cum_battery) > 0 else 0.0
    )

    # Jede Phase getrennt zählen → durchschnittliche Oszillationszeit
    osc_count = sum(
        (1 if a else 0) + (1 if b else 0) + (1 if c else 0)
        for a, b, c in zip(
            result.osc_active_a, result.osc_active_b, result.osc_active_c, strict=True
        )
    )
    oscillation_time_pct = 100.0 * osc_count / max(1, 3 * n)

    return Statistics(
        csv_file=result.csv_file,
        duration_h=(result.timestamps[-1] - result.timestamps[0]) / 3600.0,
        num_samples=n,
        mean_grid_w=mean_w,
        std_grid_w=std_w,
        min_grid_w=min(grid),
        max_grid_w=max(grid),
        time_in_band_pct=time_in_band_pct,
        total_grid_import_wh=cum_import,
        total_grid_export_wh=cum_export,
        total_battery_wh=cum_battery,
        efficiency_pct=efficiency_pct,
        oscillation_time_pct=oscillation_time_pct,
    )


# ── Simulation ────────────────────────────────────────────────────────────────


def run_simulation(
    records: List[PhaseRecord],
    config: ZeroFeedV4Config,
    csv_name: str = "",
    show_progress: bool = False,
) -> SimulationResult:
    """Führt eine V4 Simulation über CSV-Records durch.

    Verwendet simulierte Zeitstempel (CSV-Timestamps) für das PT1-Modell –
    keine echte Systemuhr, läuft in beliebiger Geschwindigkeit.
    """
    if not records:
        return SimulationResult(
            csv_file=csv_name, duration_s=0.0, num_samples=0, control_phase=config.control_phase
        )

    core = _V4Core(config)
    ctrl_ph = config.control_phase
    dead_time = config.battery_dead_time_s
    tau = config.battery_pt1_tau_s

    # PT1-Zustand
    sp_sent_at: float = 0.0
    sp_target_w: float = 0.0
    sp_prev_w: float = 0.0
    current_setpoint: int = 0
    last_control_ts: float = 0.0

    # Sample-Buffer (wird nach jedem Control-Zyklus geleert)
    phase_buf: Dict[str, List[PhaseSample]] = {"A": [], "B": [], "C": []}
    batt_buf: List[float] = []

    # Zuletzt berechnete Werte (gehalten zwischen Control-Zyklen)
    last_ff: Dict[str, float] = {"A": 0.0, "B": 0.0, "C": 0.0}
    last_fb: float = 0.0
    last_ff_sum: float = 0.0
    last_osc: Dict[str, bool] = {"A": False, "B": False, "C": False}

    result = SimulationResult(
        csv_file=csv_name,
        duration_s=records[-1].timestamp - records[0].timestamp,
        num_samples=len(records),
        control_phase=ctrl_ph,
    )

    for i, rec in enumerate(records):
        if show_progress and i % 500 == 0:
            logger.info("Simulation %s: %d/%d", csv_name, i, len(records))

        t = rec.timestamp
        batt_est = _pt1(sp_sent_at, sp_target_w, sp_prev_w, dead_time, tau, t)

        # Batterie-Output von der control_phase abziehen (entspricht realer Hardware)
        pa = rec.phase_a - (batt_est if ctrl_ph == "A" else 0.0)
        pb = rec.phase_b - (batt_est if ctrl_ph == "B" else 0.0)
        pc = rec.phase_c - (batt_est if ctrl_ph == "C" else 0.0)

        phase_buf["A"].append(PhaseSample(timestamp=t, value=pa))
        phase_buf["B"].append(PhaseSample(timestamp=t, value=pb))
        phase_buf["C"].append(PhaseSample(timestamp=t, value=pc))
        batt_buf.append(batt_est)

        # Control-Zyklus
        if last_control_ts == 0.0 or (t - last_control_ts) >= config.control_interval_s:
            ff_outputs, fb_correction, ff_sum = core.calculate(
                phase_samples=phase_buf,
                batt_hist=batt_buf,
                current_battery_output_w=batt_est,
                battery_settled=True,
            )
            raw_target = ff_sum + fb_correction

            # Watchdog
            last_values = {ph: phase_buf[ph][-1].value for ph in ("A", "B", "C")}
            reset_phases = core.check_watchdog(last_values, ff_sum)
            if reset_phases:
                raw_target = float(config.min_output_w)
                logger.debug("Watchdog reset: %s", reset_phases)

            new_sp = int(
                round(max(float(config.min_output_w), min(float(config.max_output_w), raw_target)))
            )
            if new_sp != current_setpoint:
                sp_prev_w = batt_est
                sp_sent_at = t
                sp_target_w = float(new_sp)
                current_setpoint = new_sp

            last_ff = ff_outputs
            last_fb = fb_correction
            last_ff_sum = ff_sum
            last_osc = {ph: core.osc_state(ph).oscillating for ph in ("A", "B", "C")}

            last_control_ts = t
            phase_buf = {"A": [], "B": [], "C": []}
            batt_buf = []

        # Aufzeichnen
        result.timestamps.append(t)
        result.phase_a.append(pa)
        result.phase_b.append(pb)
        result.phase_c.append(pc)
        result.grid_total.append(pa + pb + pc)
        result.battery_output.append(batt_est)
        result.setpoints.append(float(current_setpoint))
        result.ff_a.append(last_ff.get("A", 0.0))
        result.ff_b.append(last_ff.get("B", 0.0))
        result.ff_c.append(last_ff.get("C", 0.0))
        result.fb_correction.append(last_fb)
        result.ff_sum.append(last_ff_sum)
        result.osc_active_a.append(last_osc.get("A", False))
        result.osc_active_b.append(last_osc.get("B", False))
        result.osc_active_c.append(last_osc.get("C", False))

    return result


def run_batch(
    csv_files: List[Path],
    config: ZeroFeedV4Config,
    show_progress: bool = False,
) -> List["tuple[SimulationResult, Statistics]"]:
    """Simuliert mehrere CSV-Dateien mit derselben Konfiguration."""
    results = []
    for path in csv_files:
        records = load_csv(path)
        records = clean_csv_data(records)
        if not records:
            continue
        result = run_simulation(records, config, csv_name=path.name, show_progress=show_progress)
        stats = compute_statistics(result, target_power_w=config.target_power_w)
        results.append((result, stats))
        if show_progress:
            logger.info(
                "%s: mean=%.1fW  band=%.0f%%  eff=%.0f%%",
                path.name,
                stats.mean_grid_w,
                stats.time_in_band_pct,
                stats.efficiency_pct,
            )
    return results

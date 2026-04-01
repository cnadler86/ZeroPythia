"""Test Feed-Forward Controllers – Phase A + C.

================================================

Lädt 4 Shelly-CSV-Dateien (2026-02-25 … 2026-02-28) und simuliert
den ZeroFeed-Controller mit kp=1, Hysterese=5W.

Phase B (Inverter) läuft mit, wird aber nicht bewertet – die Topologie
macht dort eine separate Betrachtung nötig.

Was geprüft wird
----------------
* PhaseController A + C:  erzeugen sie korrekte Korrekturen?
* Oszillationserkennung:  feuert sie bei schwingenden Lasten?
* Gesamtbilanz:           bleibt Total-Grid nahe 0?

Usage::

    python utils/test_ff_controllers.py [--csv-dir PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import statistics as stat_mod
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

# ── Workspace-Root zum sys.path hinzufügen ─────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from clients.zendure.mock.async_mock_client import SolarFlowAsyncMockClient
from simulator.grid_simulator import GridSimulator, clean_csv_data, load_csv
from src.controller.oscillation_detectorv2 import (
    BaseloadHolderSettings,
    BaseloadPredictorSettings,
)
from src.controller.phase_controller import (
    InverterPhaseControllerSettings,
    PhaseControllerSettings,
    ZeroFeedManagerSettings,
)
from src.controller.zerofeed_v3 import PhaseSample, ZeroFeedV3Controller, ZeroFeedV3Settings

# ─────────────────────────────────────────────────────────────────────────────
# Ergebnis-Datenklassen
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CSV_DIR = Path(r"C:\Privat\shelly_logger\ShellyLog")
CSV_FILES = [
    "shelly3em_power_2026-02-25.csv",
    "shelly3em_power_2026-02-26.csv",
    "shelly3em_power_2026-02-27.csv",
    "shelly3em_power_2026-02-28.csv",
]


@dataclass
class PhaseStats:
    """Statistiken für eine einzelne Phase."""

    name: str
    raw_values: List[float] = field(default_factory=list)
    """Rohleistung vom Messgerät (vor Kompensation)."""
    corrections: List[float] = field(default_factory=list)
    """Korrekturen des Phase-Controllers."""
    osc_limits: List[float] = field(default_factory=list)
    """Oszillations-Limits über die Zeit."""

    @property
    def mean_raw(self) -> float:
        return stat_mod.mean(self.raw_values) if self.raw_values else 0.0

    @property
    def std_raw(self) -> float:
        return stat_mod.stdev(self.raw_values) if len(self.raw_values) > 1 else 0.0

    @property
    def mean_correction(self) -> float:
        return stat_mod.mean(self.corrections) if self.corrections else 0.0

    @property
    def osc_pct(self) -> float:
        """Prozentualer Anteil der Zeit mit aktivem Oszillations-Limit."""
        if not self.osc_limits:
            return 0.0
        n_osc = sum(1 for v in self.osc_limits if v < float("inf"))
        return 100.0 * n_osc / len(self.osc_limits)

    @property
    def min_osc_limit(self) -> float:
        active = [v for v in self.osc_limits if v < float("inf")]
        return min(active) if active else float("inf")


@dataclass
class FileResult:
    """Simulationsergebnis für eine CSV-Datei."""

    csv_name: str
    duration_h: float
    num_samples: int

    phase_a: PhaseStats = field(default_factory=lambda: PhaseStats("A"))
    phase_c: PhaseStats = field(default_factory=lambda: PhaseStats("C"))

    total_grid: List[float] = field(default_factory=list)
    battery_output: List[float] = field(default_factory=list)
    setpoints: List[float] = field(default_factory=list)

    @property
    def mean_total(self) -> float:
        return stat_mod.mean(self.total_grid) if self.total_grid else 0.0

    @property
    def std_total(self) -> float:
        return stat_mod.stdev(self.total_grid) if len(self.total_grid) > 1 else 0.0

    @property
    def pct_in_band(self) -> float:
        """Prozentualer Anteil der Zeit mit |Total-Grid| ≤ 20W."""
        if not self.total_grid:
            return 0.0
        in_band = sum(1 for v in self.total_grid if abs(v) <= 20)
        return 100.0 * in_band / len(self.total_grid)

    @property
    def pct_feed_in(self) -> float:
        """Prozentualer Anteil mit Einspeisung (Total < 0)."""
        if not self.total_grid:
            return 0.0
        feed_in = sum(1 for v in self.total_grid if v < 0)
        return 100.0 * feed_in / len(self.total_grid)

    @property
    def total_battery_wh(self) -> float:
        if len(self.battery_output) < 2:
            return 0.0
        total = 0.0
        for i, p in enumerate(self.battery_output[1:], 1):
            # Annahme: ~1s zwischen Samples
            total += p / 3600.0
        return total

    # ── AC-Residual: (Phase_A + Phase_C) - battery_output ────────────
    # Positiv = noch Netzbezug auf A+C. Negativ = überkompensiert (Einspeisung auf B).
    ac_residual: List[float] = field(default_factory=list)

    @property
    def mean_ac_residual(self) -> float:
        return stat_mod.mean(self.ac_residual) if self.ac_residual else 0.0

    @property
    def pct_ac_feed_in(self) -> float:
        """Anteil mit Einspeisung auf A+C-Ebene (Residual < 0)."""
        if not self.ac_residual:
            return 0.0
        return 100.0 * sum(1 for v in self.ac_residual if v < 0) / len(self.ac_residual)

    @property
    def pct_ac_in_band(self) -> float:
        """Anteil mit |AC-Residual| ≤ 20 W."""
        if not self.ac_residual:
            return 0.0
        return 100.0 * sum(1 for v in self.ac_residual if abs(v) <= 20) / len(self.ac_residual)


# ─────────────────────────────────────────────────────────────────────────────
# Einstellungen für Test
# ─────────────────────────────────────────────────────────────────────────────


def make_settings(
    kp: float = 1.0,
    hysteresis_w: float = 8.0,
    target_w: float = 3.0,
    ff_only: bool = True,
) -> ZeroFeedV3Settings:
    """Erstellt Controller-Einstellungen.

    Args:
        ff_only: True = Phase-B-Feedback deaktiviert (kp=0). Reiner A+C-Feedforward-Test.
                 False = Vollständiges System mit Phase-B-Feedback.
    """
    inverter_kp = 0.0 if ff_only else 0.95
    inverter_kp_fi = 0.0 if ff_only else 1.05
    return ZeroFeedV3Settings(
        manager=ZeroFeedManagerSettings(
            min_output_w=20,
            max_output_w=800,
            min_change_w=3.0,
        ),
        phase_controller=PhaseControllerSettings(
            kp=kp,
            hysteresis_w=hysteresis_w,
            kp_hysteresis=0.3,
            target_power_w=target_w,
        ),
        inverter_controller=InverterPhaseControllerSettings(
            kp_draw=inverter_kp,
            kp_feed_in=inverter_kp_fi,
            kp_hysteresis=0.0,
            hysteresis_w=hysteresis_w,
            target_power_w=5.0,
            min_output_w=20.0,
            max_output_w=800.0,
            preprocessing_hysteresis_w=hysteresis_w,
        ),
        # Oszillationserkennung aktivieren
        holder_settings=BaseloadHolderSettings(),
        predictor_settings=BaseloadPredictorSettings(),
        sampling_interval=1.0,
        control_interval_s=3.0,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Simulation
# ─────────────────────────────────────────────────────────────────────────────


async def simulate_file(
    csv_path: Path,
    settings: ZeroFeedV3Settings,
    *,
    verbose: bool = False,
    ff_only: bool = False,
) -> FileResult:
    """Simuliert eine CSV-Datei und gibt detaillierte Ergebnisse zurück.

    Args:
        ff_only: True = Regelzyklus nur über Phase A+C Feedforward (Phase-B-Feedback
                 ist intern kp=0 aber würde den Setpoint gegensteuern).
                 Für den A/C-Sweep immer True setzen.
    """
    print(f"  Lade {csv_path.name} ...", end="", flush=True)
    records = load_csv(csv_path)
    records = clean_csv_data(records)
    print(f" {len(records):,} Samples", flush=True)

    if not records:
        return FileResult(csv_name=csv_path.name, duration_h=0, num_samples=0)

    duration_h = (records[-1].timestamp - records[0].timestamp) / 3600.0

    # Mock-Batterie + GridSimulator
    battery = SolarFlowAsyncMockClient(
        initial_soc=100,
        battery_capacity_wh=100_000,  # Keine Energielimitierung im Test
    )
    grid_sim = GridSimulator(records=records, battery_mock=battery)

    # Controller
    controller = ZeroFeedV3Controller(
        settings=settings,
        grid_meter=grid_sim,
        battery=battery,
    )

    # Ergebnis
    result = FileResult(
        csv_name=csv_path.name,
        duration_h=duration_h,
        num_samples=len(records),
    )

    # ── Simulation initialisieren ────────────────────────────────────
    sim_time = records[0].timestamp
    battery.set_simulation_time(sim_time)
    grid_sim.set_simulation_time(sim_time)

    min_out = settings.manager.min_output_w
    await battery.start_discharge(min_out)
    for _ in range(20):
        sim_time += 0.5
        battery.set_simulation_time(sim_time)
        pwr = await battery.get_ac_output_power()
        if pwr is not None and pwr >= min_out:
            break

    controller._current_output_limit = min_out

    # ── Hauptschleife ────────────────────────────────────────────────
    control_interval = settings.control_interval_s
    last_control_time = sim_time

    # Sample-Akkumulatoren zwischen Control-Zyklen (für Phase-Controller-Input)
    phase_a_buf: List[float] = []
    phase_c_buf: List[float] = []

    print("  Simuliere ...", end="", flush=True)
    dot_every = max(1, len(records) // 20)

    for i, rec in enumerate(records):
        if i % dot_every == 0:
            print(".", end="", flush=True)

        sim_time = rec.timestamp
        battery.set_simulation_time(sim_time)
        grid_sim.set_simulation_time(sim_time)

        phases = await grid_sim.get_phase_powers()
        if phases is None:
            continue

        phase_a, phase_b, phase_c = phases
        # get_grid_output_power() ist sync und verwendet Simulationszeit direkt –
        # dasselbe, was der GridSimulator von PhaseB abzieht.
        output_power = battery.get_grid_output_power()

        sample = PhaseSample(
            timestamp=sim_time,
            phase_a=phase_a,
            phase_b=phase_b,
            phase_c=phase_c,
            battery_output=output_power,
        )
        await controller.add_sample(sample)

        phase_a_buf.append(phase_a)
        phase_c_buf.append(phase_c)

        # Regelzyklus
        if sim_time - last_control_time >= control_interval:
            if ff_only:
                # Direkt A+C-Korrekturen berechnen (Phase B wird neutralisiert).
                # Die Manager-Architektur wrde Phase B als Gegenspieler wirken
                # lassen wenn kp=0 aber min_output>0 gesetzt ist.
                ctrl_a = controller.manager._phase_a
                ctrl_c = controller.manager._phase_c

                # Oscillation-Samples einfüttern (macht add_sample schon)
                res_a = ctrl_a.calculate(phase_a_buf[:])
                res_c = ctrl_c.calculate(phase_c_buf[:])

                correction = res_a.correction_w + res_c.correction_w
                new_sp = int(
                    max(
                        settings.manager.min_output_w,
                        min(settings.manager.max_output_w, round(correction)),
                    )
                )
                if abs(new_sp - controller.current_output_limit) >= settings.manager.min_change_w:
                    await battery.set_ac_output_limit(new_sp)
                    controller._current_output_limit = new_sp
            else:
                await controller.perform_control()
            last_control_time = sim_time
            phase_a_buf = []
            phase_c_buf = []

        # ── Per-Phase Daten aufzeichnen ──────────────────────────────
        ctrl_a = controller.manager._phase_a
        ctrl_c = controller.manager._phase_c

        result.phase_a.raw_values.append(phase_a)
        result.phase_a.corrections.append(ctrl_a.last_output)
        result.phase_a.osc_limits.append(
            ctrl_a.get_osc_limit() if ctrl_a.is_oscillating else float("inf")
        )

        result.phase_c.raw_values.append(phase_c)
        result.phase_c.corrections.append(ctrl_c.last_output)
        result.phase_c.osc_limits.append(
            ctrl_c.get_osc_limit() if ctrl_c.is_oscillating else float("inf")
        )

        result.total_grid.append(phase_a + phase_b + phase_c)
        result.battery_output.append(output_power)
        result.setpoints.append(float(controller.current_output_limit))

        # AC-Residual: wie viel A+C nach Kompensation übrig bleibt.
        # Batterie hängt physisch an Phase B; ihre Ausgabe reduziert den
        # Gesamtbezug. Residual = (A+C-Last) - Battery-Output.
        result.ac_residual.append((phase_a + phase_c) - output_power)

    print(" fertig")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Ausgabe
# ─────────────────────────────────────────────────────────────────────────────

SEP = "─" * 80


def _bar(value: float, max_val: float, width: int = 30, char: str = "█") -> str:
    """Einfacher ASCII-Balken."""
    filled = int(round(width * min(value, max_val) / max(max_val, 1)))
    return char * filled + "░" * (width - filled)


def print_file_result(r: FileResult) -> None:
    print(f"\n{'═' * 80}")
    print(f"  {r.csv_name}  ({r.duration_h:.1f}h, {r.num_samples:,} Samples)")
    print("═" * 80)

    # ── Phasen A und C ──────────────────────────────────────────────
    for ps in (r.phase_a, r.phase_c):
        osc_str = (
            f"OSC {ps.osc_pct:.0f}%  min-Limit={ps.min_osc_limit:.0f}W"
            if ps.osc_pct > 0
            else "kein Oszillationsereignis"
        )
        bar = _bar(ps.mean_correction, ps.mean_raw or 1)
        coverage = 100.0 * ps.mean_correction / ps.mean_raw if ps.mean_raw > 0 else 0.0
        print(
            f"  Phase {ps.name}:  "
            f"ø-Last={ps.mean_raw:6.1f}W  "
            f"σ={ps.std_raw:5.1f}W  "
            f"ø-Korrektur={ps.mean_correction:6.1f}W  "
            f"Deckung={coverage:5.1f}%"
        )
        print(f"           Korrekturbalken [{bar}]  {osc_str}")

    # ── Gesamt ──────────────────────────────────────────────────────
    print()
    print(
        f"  Total-Grid:   ø={r.mean_total:+.1f}W  σ={r.std_total:.1f}W  "
        f"im Band(±20W)={r.pct_in_band:.0f}%  "
        f"Einspeisung={r.pct_feed_in:.1f}%"
    )
    print(
        f"  AC-Residual:  ø={r.mean_ac_residual:+.1f}W  "
        f"im Band(±20W)={r.pct_ac_in_band:.0f}%  "
        f"AC-Einspeisung={r.pct_ac_feed_in:.1f}%"
        f"  (= (A+C) - Batterie)"
    )
    mean_sp = stat_mod.mean(r.setpoints) if r.setpoints else 0
    print(f"  Batterie:     ø-Setpoint={mean_sp:.0f}W  Gesamt≈{r.total_battery_wh:.0f}Wh entladen")


def print_summary(results: List[FileResult]) -> None:
    print(f"\n{'═' * 80}")
    print("  ZUSAMMENFASSUNG  (AC-Residual = (A+C-Last) − Batterie-Output)")
    print("═" * 80)
    print(
        f"  {'Datei':<40} {'AC-Resø':>8} {'AC-Einsp%':>10} {'AC-Band%':>9} "
        f"{'A-Dck%':>7} {'C-Dck%':>7} {'OscA%':>6} {'OscC%':>6}"
    )
    print(f"  {'-' * 40} {'-' * 8} {'-' * 10} {'-' * 9} {'-' * 7} {'-' * 7} {'-' * 6} {'-' * 6}")

    for r in results:
        cov_a = (
            100.0 * r.phase_a.mean_correction / r.phase_a.mean_raw
            if r.phase_a.mean_raw > 0
            else 0.0
        )
        cov_c = (
            100.0 * r.phase_c.mean_correction / r.phase_c.mean_raw
            if r.phase_c.mean_raw > 0
            else 0.0
        )
        print(
            f"  {r.csv_name:<40} "
            f"{r.mean_ac_residual:>+8.1f} "
            f"{r.pct_ac_feed_in:>10.1f} "
            f"{r.pct_ac_in_band:>9.0f} "
            f"{cov_a:>7.0f} "
            f"{cov_c:>7.0f} "
            f"{r.phase_a.osc_pct:>6.0f} "
            f"{r.phase_c.osc_pct:>6.0f}"
        )
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Einstiegspunkt
# ─────────────────────────────────────────────────────────────────────────────


async def main(
    csv_dir: Path,
    kp: float,
    hysteresis_w: float,
    target_w: float,
    verbose: bool,
) -> None:
    settings = make_settings(kp=kp, hysteresis_w=hysteresis_w, target_w=target_w, ff_only=True)

    print(SEP)
    print("  Zero-Feed Feed-Forward Controller Test  [Phase-B-Feedback deaktiviert]")
    print(
        f"  kp={kp}  hysteresis={hysteresis_w}W  target={target_w}W"
        f"  control_interval={settings.control_interval_s}s"
    )
    print(SEP)

    results: List[FileResult] = []
    for name in CSV_FILES:
        p = csv_dir / name
        if not p.exists():
            print(f"  [SKIP] {name} – nicht gefunden in {csv_dir}")
            continue
        r = await simulate_file(p, settings, verbose=verbose, ff_only=True)
        print_file_result(r)
        results.append(r)

    if results:
        print_summary(results)


# ─────────────────────────────────────────────────────────────────────────────
# Parameter-Sweep  (--optimize)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class SweepPoint:
    hysteresis_w: float
    target_w: float
    mean_total: float  # ø Grid-Bezug über alle Tage (Kompatibilität)
    pct_feed_in: float  # % Zeit mit Einspeisung (Kompatibilität)
    pct_in_band: float  # % Zeit im ±20W-Band (Kompatibilität)
    battery_wh: float  # Gesamt-Batterieentladung in Wh
    mean_ac_residual: float  # ø AC-Residual = (A+C) - Batterie
    pct_ac_feed_in: float  # % Zeit mit AC-Überkompenation (< 0)
    pct_ac_in_band: float  # % Zeit |AC-Residual| ≤ 20W

    @property
    def score(self) -> float:
        """Kombinierter Score auf AC-Residual-Basis (höher = besser).

        Straft AC-Einspeisung stark (×15) und belohnt:
        - Band-Abdeckung (% Zeit AC-Residual ≤ 20W)
        - Kleine positive Residual (kaum Restbezug)
        Kein Bonus für negatives Residual (Überkompenation = schlecht).
        """
        residual_penalty = max(0.0, -self.mean_ac_residual) * 2.0
        return self.pct_ac_in_band - 15.0 * self.pct_ac_feed_in - residual_penalty


async def run_sweep(csv_dir: Path, kp: float) -> None:
    hysteresis_range = [3.0, 5.0, 8.0, 12.0, 15.0]
    target_range = [0.0, 3.0]

    # Vorhandene CSV-Pfade bestimmen
    csv_paths = [csv_dir / n for n in CSV_FILES if (csv_dir / n).exists()]
    if not csv_paths:
        print(f"  Keine CSV-Dateien gefunden in {csv_dir}")
        return

    total_combos = len(hysteresis_range) * len(target_range)
    print(SEP)
    print(f"  Parameter-Sweep  ({total_combos} Kombinationen × {len(csv_paths)} Tage)")
    print(f"  kp={kp}  Hysterese: {hysteresis_range}  Ziel: {target_range}")
    print("  Metrik: AC-Residual = (Phase_A + Phase_C) − Batterie-Output  [Phase-B-FB deaktiviert]")
    print(SEP)

    sweep_results: List[SweepPoint] = []

    for h in hysteresis_range:
        for t in target_range:
            settings = make_settings(kp=kp, hysteresis_w=h, target_w=t, ff_only=True)
            day_results: List[FileResult] = []
            print(f"\n  h={h:5.1f}W  target={t:.0f}W  ", end="", flush=True)
            for p in csv_paths:
                r = await simulate_file(p, settings, verbose=False, ff_only=True)
                day_results.append(r)

            # Aggregat über alle Tage – AC-Residual-Metriken
            mean_ac = stat_mod.mean(r.mean_ac_residual for r in day_results)
            pct_ac_feed_in = stat_mod.mean(r.pct_ac_feed_in for r in day_results)
            pct_ac_band = stat_mod.mean(r.pct_ac_in_band for r in day_results)
            battery_wh = sum(r.total_battery_wh for r in day_results)

            sp = SweepPoint(
                hysteresis_w=h,
                target_w=t,
                mean_total=mean_ac,
                pct_feed_in=pct_ac_feed_in,
                pct_in_band=pct_ac_band,
                battery_wh=battery_wh,
                mean_ac_residual=mean_ac,
                pct_ac_feed_in=pct_ac_feed_in,
                pct_ac_in_band=pct_ac_band,
            )
            sweep_results.append(sp)
            print(
                f"  AC-Res={mean_ac:+.1f}W  AC-Einsp={pct_ac_feed_in:.1f}%"
                f"  AC-Band={pct_ac_band:.0f}%  Batt={battery_wh:.0f}Wh"
                f"  Score={sp.score:.1f}"
            )

    # Tabelle sortiert nach Score
    sweep_results.sort(key=lambda x: x.score, reverse=True)

    print(f"\n{'═' * 80}")
    print("  SWEEP-ERGEBNIS (sortiert nach Score)  — AC-Residual = (A+C) - Batt")
    print("═" * 80)
    print(
        f"  {'h[W]':>6} {'Ziel[W]':>8} {'AC-Resø[W]':>11} {'AC-Einsp%':>10} "
        f"{'AC-Band%':>9} {'Batt[Wh]':>10} {'Score':>7}"
    )
    print(f"  {'-' * 6} {'-' * 8} {'-' * 11} {'-' * 10} {'-' * 9} {'-' * 10} {'-' * 7}")
    for sp in sweep_results:
        marker = " ◄ BEST" if sp is sweep_results[0] else ""
        print(
            f"  {sp.hysteresis_w:>6.1f} {sp.target_w:>8.0f} {sp.mean_ac_residual:>+11.1f}"
            f" {sp.pct_ac_feed_in:>10.1f} {sp.pct_ac_in_band:>9.0f}"
            f" {sp.battery_wh:>10.0f} {sp.score:>7.1f}{marker}"
        )

    best = sweep_results[0]
    print(f"\n  Empfehlung:  hysteresis={best.hysteresis_w}W  target={best.target_w}W")
    print(
        f"  AC-Residual ø {best.mean_ac_residual:+.1f}W  |  "
        f"AC-Einspeisung {best.pct_ac_feed_in:.1f}% der Zeit  |  "
        f"Band {best.pct_ac_in_band:.0f}%  |  "
        f"Batterie {best.battery_wh:.0f} Wh"
    )
    print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv-dir",
        type=Path,
        default=DEFAULT_CSV_DIR,
        help=f"Verzeichnis mit den Shelly-CSV-Dateien (Standard: {DEFAULT_CSV_DIR})",
    )
    parser.add_argument("--kp", type=float, default=1.0, help="Verstärkung (Standard: 1.0)")
    parser.add_argument(
        "--hysteresis", type=float, default=8.0, help="Hysterese in Watt (Standard: 8.0)"
    )
    parser.add_argument(
        "--target", type=float, default=3.0, help="Ziel-Bezug in Watt (Standard: 3.0)"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Ausführliche Ausgabe")
    parser.add_argument(
        "--optimize",
        action="store_true",
        help="Parameter-Sweep über Hysterese und Zielwert",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.optimize:
        asyncio.run(run_sweep(args.csv_dir, args.kp))
    else:
        asyncio.run(main(args.csv_dir, args.kp, args.hysteresis, args.target, args.verbose))

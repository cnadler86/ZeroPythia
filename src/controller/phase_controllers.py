"""Phasen-getrennte Controller für Zero-Feed.
==========================================

Architektur:
- Phase A+C (Fremdphasen, KEIN Inverter): schneller Feedforward-Regler
  → Reine Störgröße, keine Rückkopplung → sofortige volle Kompensation
  → Läuft in jedem Regelzyklus neu

- Phase B (Batterie-Phase): Feedback-Regler mit Settlement-Gate
  → Neuer Feedback-Setpoint wird NUR berechnet wenn der Inverter
    am vorherigen Setpoint angekommen ist (is_settled() == True)
  → Verhindert Kreisoscillation: Regler wartet auf die Batterie-Totzeit
    (~2-3s) bevor er nachregelt
  → Solange nicht settled: Feedback eingefroren, nur Feedforward aktiv

- PhaseManager: Kombiniert beide Ausgänge zu einem Batterie-Setpoint
"""

import logging
from dataclasses import dataclass
from typing import NamedTuple

from .pre_processor import HysteresisPreprocessor

logger = logging.getLogger(__name__)


# ── Einstellungen ─────────────────────────────────────────────────────────────


@dataclass
class DisturbanceControllerSettings:
    """Einstellungen für den Fremdphasen-Controller (A+C)."""

    kp: float = 1.0
    """Verstärkung: 1.0 = volle Kompensation der Fremdphasen-Last"""

    hysteresis_w: float = 10.0
    """Hysterese in Watt – innerhalb dieser Zone wird nicht nachgeregelt"""

    kp_hysteresis: float = 0.3
    """Gedämpfter Kp innerhalb der Hysterese"""

    max_compensation_w: float = 800.0
    """Maximale Kompensation in Watt (Batterie-Limit)"""

    preprocessing_hysteresis_w: float = 10.0
    """Hysterese für den Preprocessor"""


@dataclass
class BatteryPhaseControllerSettings:
    """Einstellungen für den Batterie-Phasen-Controller (Phase B)."""

    kp_draw: float = 0.95
    """Verstärkung bei Netzbezug (vorsichtiger)"""

    kp_feed_in: float = 1.05
    """Verstärkung bei Einspeisung (aggressiver)"""

    hysteresis_w: float = 10.0
    """Hysterese in Watt"""

    kp_hysteresis: float = 0.3
    """Gedämpfter Kp innerhalb der Hysterese"""

    target_power_w: float = 5.0
    """Ziel-Leistung Phase B in Watt (leichter Bezug)"""

    min_output_w: float = 0.0
    """Minimaler Output (0 = kein Limit von dieser Seite)"""

    max_output_w: float = 800.0
    """Maximaler Output"""

    preprocessing_hysteresis_w: float = 10.0
    """Hysterese für den Preprocessor"""


@dataclass
class PhaseManagerSettings:
    """Einstellungen für den Phase-Manager."""

    min_output_w: int = 20
    """Minimaler Gesamtoutput (Batterie-Minimum)"""

    max_output_w: int = 800
    """Maximaler Gesamtoutput (Batterie-Maximum)"""

    target_total_grid_w: float = 5.0
    """Gesamtziel: leichter Netzbezug über alle Phasen"""

    min_change_w: float = 3.0
    """Minimale Setpoint-Änderung – kleinere Änderungen werden ignoriert"""


# ── Controller ────────────────────────────────────────────────────────────────


class DisturbanceController:
    """Feedforward-Controller für Fremdphasen (A+C).

    Beobachtet die Last auf Phasen ohne Batterie und berechnet,
    wie viel zusätzliche Batterieleistung nötig ist um diese zu kompensieren.

    Kein Stabilitätsrisiko, da keine Rückkopplung: Die Batterie-Ausgabe
    ändert nur Phase B, nicht Phase A/C.
    """

    def __init__(self, settings: DisturbanceControllerSettings):
        self.settings = settings
        self.preprocessor = HysteresisPreprocessor(hysteresis=settings.preprocessing_hysteresis_w)
        self._last_output: float = 0.0

    def calculate(self, other_phases_power_w: list[float]) -> float:
        """Berechnet Kompensations-Leistung für Fremdphasen.

        Args:
            other_phases_power_w: Liste der letzten Messwerte (A+C Summe).
                Positiv = Bezug, Negativ = Einspeisung.

        Returns:
            Gewünschte Batterie-Kompensation in Watt (positiv = entladen).
            Dieser Wert wird zum Phase-B-Controller-Output addiert.
        """
        if not other_phases_power_w:
            return self._last_output

        filtered = self.preprocessor.process(other_phases_power_w)
        if filtered is None:
            return self._last_output

        # Feedforward: Kompensiere den Bezug auf anderen Phasen
        # Positiver Bezug → mehr Batterie-Output nötig
        error = filtered  # Bezug auf Fremdphasen
        if abs(error) < self.settings.hysteresis_w:
            compensation = self.settings.kp_hysteresis * error
        else:
            compensation = self.settings.kp * error

        # Begrenze auf [0, max] – wir kompensieren nur Bezug, nicht Einspeisung
        compensation = max(0.0, min(self.settings.max_compensation_w, compensation))
        self._last_output = compensation
        return compensation

    @property
    def last_output(self) -> float:
        return self._last_output


class BatteryPhaseController:
    """Asymmetrischer P-Regler für die Batterie-Phase (B).

    Regelt auf TOTAL GRID POWER (Summe aller Phasen), nicht auf Phase B allein.
    Phase B ist zwar die Stellphase, aber durch Saldierung zählt nur die Summe.
    Phase B wird bei Fremdphasen-Kompensation negativ (feed-in) - das ist korrekt.

    Strategie:
    - Bei Einspeisung (total < target): Aggressiv reduzieren (kp_feed_in > 1)
    - Bei Bezug (total > target): Vorsichtig erhöhen (kp_draw < 1)
    - Hysterese-Zone: Sanfte Regelung (kp_hysteresis)
    """

    def __init__(self, settings: BatteryPhaseControllerSettings):
        self.settings = settings
        self.preprocessor = HysteresisPreprocessor(hysteresis=settings.preprocessing_hysteresis_w)
        self._last_output: float = 0.0

    def calculate(
        self,
        total_grid_power_w: list[float],
        current_battery_output_w: float,
    ) -> float:
        """Berechnet gewünschte Batterie-Leistung basierend auf Gesamt-Grid.

        Args:
            total_grid_power_w: Liste der letzten Messwerte TOTAL Grid (alle Phasen).
                Positiv = Bezug, Negativ = Einspeisung.
            current_battery_output_w: Aktueller Batterie-Output.

        Returns:
            Gewünschter Batterie-Output in Watt (positiv = entladen).
        """
        if not total_grid_power_w:
            return self._last_output

        filtered = self.preprocessor.process(total_grid_power_w)
        if filtered is None:
            return self._last_output

        # Asymmetrische Korrektur basierend auf Total-Grid-Error
        error = filtered - self.settings.target_power_w  # positiv = zu viel Bezug

        if abs(error) < self.settings.hysteresis_w:
            correction = self.settings.kp_hysteresis * error
        elif error > 0:
            # Bezug → vorsichtig erhöhen
            correction = self.settings.kp_draw * error
        else:
            # Einspeisung → aggressiv reduzieren
            correction = self.settings.kp_feed_in * error

        new_output = current_battery_output_w + correction
        new_output = max(
            self.settings.min_output_w,
            min(self.settings.max_output_w, new_output),
        )

        self._last_output = new_output
        return new_output

    @property
    def last_output(self) -> float:
        return self._last_output


class PhaseManager:
    """Kombiniert Feedforward (A+C) + Feedback (total grid) zu einem Batterie-Setpoint.

    Architektur (Saldierung):
    - Die Batterie hängt an Phase B
    - Der Stromzähler misst die SUMME aller Phasen (Saldierung)
    - Ziel: Summe ≈ target (leichter Bezug)
    - Disturbance = vorausschauende Kompensation der A+C-Last
    - Battery Ctrl = Feedback-Regelung auf Total Grid

    Der Feedback-Controller regelt auf die Gesamtleistung.
    Der Feedforward beschleunigt die Reaktion auf A+C-Lastsprünge.
    """

    def __init__(
        self,
        manager_settings: PhaseManagerSettings,
        disturbance_settings: DisturbanceControllerSettings,
        battery_phase_settings: BatteryPhaseControllerSettings,
    ):
        self.settings = manager_settings
        self.disturbance_ctrl = DisturbanceController(disturbance_settings)
        self.battery_ctrl = BatteryPhaseController(battery_phase_settings)
        self._last_setpoint: float = 0.0
        self._last_battery_feedback: float = 0.0

    def calculate(
        self,
        total_grid_power_w: list[float],
        other_phases_power_w: list[float],
        current_battery_output_w: float,
        battery_settled: bool = True,
    ) -> int:
        """Berechnet den kombinierten Batterie-Setpoint.

        Args:
            total_grid_power_w: Messwerte Total Grid (neueste zuletzt).
            other_phases_power_w: Messwerte Phase A+C Summe (neueste zuletzt).
            current_battery_output_w: Aktueller Batterie-Output.
            battery_settled: True wenn der Inverter den letzten Setpoint erreicht hat.
                Bei False wird Feedback eingefroren, nur Feedforward (A+C) aktualisiert.

        Returns:
            Batterie-Setpoint in Watt (int, begrenzt auf Batterie-Limits).
        """
        # Feedforward (Phase A+C): läuft immer – kein Stabilitätsproblem
        disturbance_ff = self.disturbance_ctrl.calculate(other_phases_power_w)

        # Feedback (Phase B / Total): nur wenn Inverter am Setpoint angekommen
        if battery_settled:
            feedback_output = self.battery_ctrl.calculate(
                total_grid_power_w, current_battery_output_w
            )
            self._last_battery_feedback = feedback_output
        else:
            feedback_output = self._last_battery_feedback
            logger.debug(
                "PhaseManager: Inverter nicht settled – Feedback eingefroren bei %.0fW",
                feedback_output,
            )

        # Kombination: max() als stabile untere Schranke.
        # Feedforward sorgt für schnelle Reaktion auf Fremdphasen-Lastsprünge.
        # Feedback konvergiert langsamer, darf aber höher gehen (Gesamt-Einspeisung).
        total = max(feedback_output, disturbance_ff)

        # Batterie-Limits anwenden
        clamped = int(
            round(
                max(
                    self.settings.min_output_w,
                    min(self.settings.max_output_w, total),
                )
            )
        )

        # Minimum-Änderungs-Schwellwert: Ignoriere kleine Setpoint-Änderungen
        if abs(clamped - self._last_setpoint) < self.settings.min_change_w:
            clamped = int(self._last_setpoint)
        else:
            self._last_setpoint = clamped

        logger.debug(
            "PhaseManager: feedback=%.0fW  ff=%.0fW  settled=%s → total=%.0fW → clamped=%dW",
            feedback_output,
            disturbance_ff,
            battery_settled,
            total,
            clamped,
        )

        return clamped

    def calculate_debug(
        self,
        total_grid_power_w: list[float],
        other_phases_power_w: list[float],
        current_battery_output_w: float,
        battery_settled: bool = True,
    ) -> tuple[int, "PhaseManagerDebug"]:
        """Wie calculate(), gibt aber zusätzlich die Zwischenwerte zurück.

        Returns:
            (setpoint_w, PhaseManagerDebug)
        """
        # Feedforward (Phase A+C): läuft immer
        disturbance_ff = self.disturbance_ctrl.calculate(other_phases_power_w)

        # Feedback (Phase B / Total): nur wenn Inverter settled
        if battery_settled:
            feedback_output = self.battery_ctrl.calculate(
                total_grid_power_w, current_battery_output_w
            )
            self._last_battery_feedback = feedback_output
        else:
            feedback_output = self._last_battery_feedback

        total = max(feedback_output, disturbance_ff)
        clamped = int(
            round(
                max(
                    self.settings.min_output_w,
                    min(self.settings.max_output_w, total),
                )
            )
        )
        if abs(clamped - self._last_setpoint) < self.settings.min_change_w:
            clamped = int(self._last_setpoint)
        else:
            self._last_setpoint = clamped

        debug = PhaseManagerDebug(
            feedback_output_w=feedback_output,
            ff_output_w=disturbance_ff,
            raw_setpoint_w=clamped,
        )
        return clamped, debug


class PhaseManagerDebug(NamedTuple):
    """Zwischenwerte eines calculate()-Aufrufs für Logging/Analyse."""

    feedback_output_w: float
    ff_output_w: float
    raw_setpoint_w: int
    """Setpoint vor Oszillations-Limit, aber nach Batterie-Limits und min_change."""

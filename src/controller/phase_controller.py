"""Gekapselte Phasen-Controller für Zero-Feed.

=============================================

Architektur - jeder Phasen-Controller kapselt:
  1. Preprocessor  (Hysterese-basierte Sample-Filterung)
  2. P-Regler      (Feedforward oder Feedback)
  3. Oszillationsdetektoren (Holder + Predictor)

Eingabe:  Samples vom Energiemessgerät pro Phase
Ausgabe:  Korrekturwert (gewünschte Batterie-Kompensation für diese Phase)

PhaseController (Phasen OHNE Inverter, z.B. A+C):
  Reiner Feedforward.  Beobachtet Netzbezug → fordert Batterie-Kompensation.
  Kein Stabilitätsrisiko, da Batterie diese Phase nicht beeinflusst.

InverterPhaseController (Phase MIT Inverter, z.B. B):
  Feedback-Regler auf Total-Grid-Power (Saldierung).
  Berücksichtigt Korrekturen anderer Phasen für korrekte Zerlegung.
  Oszillationserkennung auf Realverbrauch (Grid + Batterie).

ZeroFeedManager:
  Addiert alle Phasen-Korrekturen → finaler Batterie-Setpoint.
  Appliziert Batterie-Grenzen (min/max) und Min-Change-Schwelle.
  Optionaler Gesamt-Oszillationsdetektor als Sicherheitsnetz.
"""

import logging
from dataclasses import dataclass
from typing import NamedTuple, Optional

from .oscillation_detectorv2 import (
    BaseloadHolder,
    BaseloadHolderSettings,
    BaseloadPredictor,
    BaseloadPredictorSettings,
)
from .pre_processor import HysteresisPreprocessor

logger = logging.getLogger(__name__)


# ── Einstellungen ─────────────────────────────────────────────────────────────


@dataclass
class PhaseControllerSettings:
    """Einstellungen für Feedforward-Phasen-Controller (ohne Inverter)."""

    kp: float = 1.0
    """Verstärkung: 1.0 = volle Kompensation des Netzbezugs"""

    hysteresis_w: float = 8.0
    """Hysterese in Watt – innerhalb dieser Zone gedämpfte Regelung"""

    kp_hysteresis: float = 0.3
    """Gedämpfter Kp innerhalb der Hysterese"""


@dataclass
class InverterPhaseControllerSettings:
    """Einstellungen für Feedback-Phasen-Controller (mit Inverter)."""

    kp_draw: float = 0.95
    """Verstärkung bei Netzbezug (vorsichtiger)"""

    kp_feed_in: float = 1.05
    """Verstärkung bei Einspeisung (aggressiver)"""

    hysteresis_w: float = 10.0
    """Hysterese in Watt"""

    kp_hysteresis: float = 0.3
    """Gedämpfter Kp innerhalb der Hysterese"""


@dataclass
class ZeroFeedManagerSettings:
    """Einstellungen für den Zero-Feed Manager."""

    min_output_w: int = 20
    """Minimaler Batterie-Output (Hardware-Limit)"""

    max_output_w: int = 800
    """Maximaler Batterie-Output (Hardware-Limit)"""

    target_power_w: float = 3.0
    """Ziel-Bezug in Watt – wir kompensieren nur, was diesen Wert übersteigt.
    Positive Werte reduzieren Einspeisung auf Kosten eines kleinen permanenten Bezugs."""


# ── Ergebnis-Typen ────────────────────────────────────────────────────────────


@dataclass
class PhaseResult:
    """Ergebnis eines Phasen-Controllers."""

    controller_output_w: float
    """Roher Controller-Output (vor Oszillations-Limit)."""

    osc_limit_w: float
    """Oszillations-Limit (inf wenn nicht oszillierend)."""

    correction_w: float
    """Effektiver Korrekturwert (nach Oszillations-Limit)."""


class ManagerDebugInfo(NamedTuple):
    """Zwischenwerte für Logging/Analyse."""

    feedback_output_w: float
    """Desired-Total vom Inverter-Controller."""

    ff_output_w: float
    """Summe der Feedforward-Korrekturen (A+C)."""

    raw_setpoint_w: int
    """Setpoint vor Gesamt-Oszillations-Limit."""

    osc_limit_w: float
    """Gesamt-Oszillations-Limit (Total-Detektor)."""


# ── Oszillations-Hilfsmethoden ────────────────────────────────────────────────


class _OscillationMixin:
    """Gemeinsame Oszillations-Logik für beide Controller-Typen."""

    holder: Optional[BaseloadHolder]
    predictor: Optional[BaseloadPredictor]

    @property
    def is_oscillating(self) -> bool:
        return (self.holder is not None and self.holder.is_oscillating) or (
            self.predictor is not None and self.predictor.is_oscillating
        )

    def get_osc_limit(self) -> float:
        limits: list[float] = []
        if self.holder and self.holder.is_oscillating:
            limits.append(self.holder.get_limit())
        if self.predictor and self.predictor.is_oscillating:
            limits.append(self.predictor.get_limit())
        return min(limits) if limits else float("inf")

    def _feed_oscillation_detectors(self, value: float, timestamp: float) -> None:
        if value > 0:
            if self.holder:
                self.holder.add_sample(value, timestamp)
            if self.predictor:
                self.predictor.add_sample(value, timestamp)


# ── PhaseController (Feedforward, ohne Inverter) ──────────────────────────────


class PhaseController(_OscillationMixin):
    """Gekapselter Controller für eine Phase OHNE Inverter.

    Kapselt: Preprocessor, P-Regler, Oszillationsdetektoren.

    Eingabe:  Phasen-Leistung vom Energiemessgerät (positiv = Bezug).
    Ausgabe:  Korrekturwert = gewünschte Batterie-Kompensation für diese Phase.

    Feedforward: Kein Stabilitätsrisiko, da Batterie diese Phase nicht
    beeinflusst (Batterie hängt an einer anderen Phase).
    """

    def __init__(
        self,
        settings: PhaseControllerSettings,
        holder_settings: Optional[BaseloadHolderSettings] = None,
        predictor_settings: Optional[BaseloadPredictorSettings] = None,
    ):
        self.settings = settings
        self.preprocessor = HysteresisPreprocessor(
            hysteresis=settings.hysteresis_w  # Hysterese für Preprocessor = Regelungs-Hysterese
        )
        self.holder = BaseloadHolder(holder_settings) if holder_settings else None
        self.predictor = BaseloadPredictor(predictor_settings) if predictor_settings else None
        self._last_output: float = 0.0

    def add_sample(self, value: float, timestamp: float) -> None:
        """Füttert Oszillationsdetektoren mit Phasen-Messwert.

        Args:
            value: Phasen-Leistung in Watt (= Realverbrauch, da kein Inverter).
            timestamp: Zeitstempel.
        """
        self._feed_oscillation_detectors(value, timestamp)

    def calculate(self, phase_power_w: list[float], target_power_w: float) -> PhaseResult:
        """Berechnet Korrekturwert (Feedforward-Kompensation).

        Args:
            phase_power_w: Letzte Messwerte dieser Phase.
                Positiv = Bezug, Negativ = Einspeisung.

        Returns:
            PhaseResult mit Korrekturwert.
        """
        osc_limit = self.get_osc_limit()

        if not phase_power_w:
            return PhaseResult(self._last_output, osc_limit, min(self._last_output, osc_limit))

        filtered = self.preprocessor.process(phase_power_w)
        if filtered is None:
            return PhaseResult(self._last_output, osc_limit, min(self._last_output, osc_limit))

        # Feedforward: Kompensiere Netzbezug oberhalb des Zielwerts
        error = filtered - target_power_w
        if abs(error) < self.settings.hysteresis_w:
            compensation = self.settings.kp_hysteresis * error
        else:
            compensation = self.settings.kp * error

        # Oszillations-Limit anwenden
        correction = min(compensation, osc_limit)
        self._last_output = correction

        return PhaseResult(compensation, osc_limit, correction)

    @property
    def last_output(self) -> float:
        return self._last_output


# ── InverterPhaseController (Feedback, mit Inverter) ──────────────────────────


class InverterPhaseController(_OscillationMixin):
    """Gekapselter Controller für die Phase MIT Inverter.

    Kapselt: Preprocessor, asymmetrischer P-Regler, Oszillationsdetektoren.

    Eingabe:  Total-Grid-Power (Saldierung aller Phasen) + Batteriezustand.
    Ausgabe:  Korrekturwert = Anteil dieser Phase am Batterie-Setpoint.

    Feedback: Regelt auf Gesamt-Netzleistung (Saldierung). Das Total-Grid
    wird benutzt, weil der Stromzähler saldiert und kleine Abweichungen
    der Feedforward-Phasen hier korrigiert werden.

    Oszillationserkennung: Läuft auf Realverbrauch (Grid + Batterie),
    da der Grid-Wert durch die Batterie beeinflusst wird.
    """

    def __init__(
        self,
        settings: InverterPhaseControllerSettings,
        holder_settings: Optional[BaseloadHolderSettings] = None,
        predictor_settings: Optional[BaseloadPredictorSettings] = None,
    ):
        self.settings = settings
        self.preprocessor = HysteresisPreprocessor(hysteresis=settings.hysteresis_w)
        self.holder = BaseloadHolder(holder_settings) if holder_settings else None
        self.predictor = BaseloadPredictor(predictor_settings) if predictor_settings else None
        self._last_desired_total: float = 0.0
        self._last_output: float = 0.0

    def add_sample(self, phase_power: float, battery_output: float, timestamp: float) -> None:
        """Füttert Oszillationsdetektoren mit Realverbrauch.

        Args:
            phase_power: Grid-Leistung dieser Phase (beeinflusst durch Batterie).
            battery_output: Aktuelle Batterie-Ausgabe.
            timestamp: Zeitstempel.
        """
        real_consumption = phase_power + battery_output
        self._feed_oscillation_detectors(real_consumption, timestamp)

    def calculate(
        self,
        total_grid_power_w: list[float],
        target_power_w: float,
        current_battery_output_w: float,
        other_corrections_w: float,
        settled: bool = True,
    ) -> PhaseResult:
        """Berechnet Korrekturwert (Feedback auf Total-Grid).

        Der Korrekturwert repräsentiert den Anteil dieser Phase am
        gewünschten Batterie-Output.  Wenn alle Phasen-Korrekturen
        addiert werden, ergibt sich der gewünschte Total-Setpoint.

        Args:
            total_grid_power_w: Letzte Messwerte Total-Grid (alle Phasen).
            current_battery_output_w: Aktueller Batterie-Output.
            other_corrections_w: Summe der Korrekturen anderer Phasen.
            settled: True wenn Inverter am vorherigen Setpoint angekommen.
                Bei False wird Feedback eingefroren.

        Returns:
            PhaseResult mit Korrekturwert.
        """
        osc_limit = self.get_osc_limit()

        # Feedback nur aktualisieren wenn Inverter settled
        if settled and total_grid_power_w:
            filtered = self.preprocessor.process(total_grid_power_w)
            if filtered is not None:
                error = filtered - target_power_w

                if abs(error) < self.settings.hysteresis_w:
                    correction = self.settings.kp_hysteresis * error
                elif error > 0:
                    correction = self.settings.kp_draw * error
                else:
                    correction = self.settings.kp_feed_in * error

                self._last_desired_total = current_battery_output_w + correction
        else:
            if not settled:
                logger.debug(
                    "InverterPhaseController: nicht settled – Feedback eingefroren bei %.0fW",
                    self._last_desired_total,
                )

        # Mein Anteil = gewünschtes Total minus was andere Phasen beitragen
        my_correction = self._last_desired_total - other_corrections_w

        # Oszillations-Limit auf meinen Anteil anwenden
        effective = min(my_correction, osc_limit)
        self._last_output = effective

        return PhaseResult(self._last_desired_total, osc_limit, effective)

    @property
    def last_output(self) -> float:
        return self._last_output

    @property
    def last_desired_total(self) -> float:
        return self._last_desired_total


# ── ZeroFeedManager ───────────────────────────────────────────────────────────


class ZeroFeedManager:
    """Kombiniert Phasen-Controller zu einem Batterie-Setpoint.

    1. Berechnet Feedforward-Korrekturen (Phasen ohne Inverter)
    2. Übergibt deren Summe an den Inverter-Controller
    3. Addiert alle Korrekturen
    4. Appliziert Batterie-Grenzen und Min-Change-Schwelle
    5. Optional: Gesamt-Oszillationsdetektor als zusätzliches Limit
    """

    def __init__(
        self,
        manager_settings: ZeroFeedManagerSettings,
        phase_a: PhaseController,
        phase_b: InverterPhaseController,
        phase_c: PhaseController,
        total_holder_settings: Optional[BaseloadHolderSettings] = None,
        total_predictor_settings: Optional[BaseloadPredictorSettings] = None,
    ):
        self.settings = manager_settings
        self.phases: dict[str, PhaseController | InverterPhaseController] = {
            "A": phase_a,
            "B": phase_b,
            "C": phase_c,
        }
        self._phase_a = phase_a
        self._phase_b = phase_b
        self._phase_c = phase_c

        # Optionaler Gesamt-Detektor (Sicherheitsnetz)
        self._total_holder = (
            BaseloadHolder(total_holder_settings) if total_holder_settings else None
        )
        self._total_predictor = (
            BaseloadPredictor(total_predictor_settings) if total_predictor_settings else None
        )

        self._last_setpoint: float = 0.0
        self._last_debug: ManagerDebugInfo = ManagerDebugInfo(
            feedback_output_w=0.0, ff_output_w=0.0, raw_setpoint_w=0, osc_limit_w=float("inf")
        )

    # ── Oszillation (Total) ───────────────────────────────────────────

    def add_total_sample(self, real_consumption: float, timestamp: float) -> None:
        """Füttert den Gesamt-Oszillationsdetektor."""
        if real_consumption > 0:
            if self._total_holder:
                self._total_holder.add_sample(real_consumption, timestamp)
            if self._total_predictor:
                self._total_predictor.add_sample(real_consumption, timestamp)

    @property
    def total_is_oscillating(self) -> bool:
        return (self._total_holder is not None and self._total_holder.is_oscillating) or (
            self._total_predictor is not None and self._total_predictor.is_oscillating
        )

    def get_total_osc_limit(self) -> float:
        limits: list[float] = []
        if self._total_holder and self._total_holder.is_oscillating:
            limits.append(self._total_holder.get_limit())
        if self._total_predictor and self._total_predictor.is_oscillating:
            limits.append(self._total_predictor.get_limit())
        return min(limits) if limits else float("inf")

    @property
    def is_oscillating(self) -> bool:
        return (
            self._phase_a.is_oscillating
            or self._phase_b.is_oscillating
            or self._phase_c.is_oscillating
            or self.total_is_oscillating
        )

    # ── Regelung ──────────────────────────────────────────────────────

    def calculate(
        self,
        phase_a_power_w: list[float],
        phase_c_power_w: list[float],
        total_grid_power_w: list[float],
        current_battery_output_w: float,
        battery_settled: bool = True,
    ) -> int:
        """Berechnet den Batterie-Setpoint.

        Args:
            phase_a_power_w: Messwerte Phase A (neueste zuletzt).
            phase_c_power_w: Messwerte Phase C (neueste zuletzt).
            total_grid_power_w: Messwerte Total Grid (neueste zuletzt).
            current_battery_output_w: Aktueller Batterie-Output.
            battery_settled: True wenn Inverter am Setpoint angekommen.

        Returns:
            Batterie-Setpoint in Watt (int, begrenzt).
        """
        # 1) Feedforward-Phasen zuerst (A + C)
        result_a = self._phase_a.calculate(phase_a_power_w, self.settings.target_power_w)
        result_c = self._phase_c.calculate(phase_c_power_w, self.settings.target_power_w)
        other_corrections = result_a.correction_w + result_c.correction_w

        # 2) Inverter-Phase (Feedback) – bekommt Summe der anderen
        result_b = self._phase_b.calculate(
            total_grid_power_w,
            self.settings.target_power_w,
            current_battery_output_w,
            other_corrections,
            battery_settled,
        )

        # 3) Summe aller Korrekturen
        raw_total = result_a.correction_w + result_b.correction_w + result_c.correction_w

        # 4) Gesamt-Oszillations-Limit (Total-Detektor)
        total_osc_limit = self.get_total_osc_limit()
        limited = min(raw_total, total_osc_limit)

        # 5) Batterie-Grenzen
        clamped = int(
            round(
                max(
                    self.settings.min_output_w,
                    min(self.settings.max_output_w, limited),
                )
            )
        )

        # 6) Min-Change-Schwelle
        if abs(clamped - self._last_setpoint) < self.settings.min_output_w:
            clamped = int(self._last_setpoint)
        else:
            self._last_setpoint = clamped

        logger.debug(
            "Manager: ff=%.0fW  fb=%.0fW  settled=%s → raw=%.0fW osc=%.0fW → %dW",
            other_corrections,
            result_b.controller_output_w,
            battery_settled,
            raw_total,
            total_osc_limit,
            clamped,
        )

        # Store debug info for callers that request it
        self._last_debug = ManagerDebugInfo(
            feedback_output_w=result_b.controller_output_w,
            ff_output_w=other_corrections,
            raw_setpoint_w=int(round(raw_total)),
            osc_limit_w=total_osc_limit,
        )

        return clamped

    def calculate_debug(
        self,
        phase_a_power_w: list[float],
        phase_c_power_w: list[float],
        total_grid_power_w: list[float],
        current_battery_output_w: float,
        battery_settled: bool = True,
    ) -> tuple[int, ManagerDebugInfo]:
        """Wrapper: führt `calculate` aus und liefert zusätzlich Debug-Infos."""
        setpoint = self.calculate(
            phase_a_power_w,
            phase_c_power_w,
            total_grid_power_w,
            current_battery_output_w,
            battery_settled,
        )
        return setpoint, self._last_debug

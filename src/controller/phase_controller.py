"""Gekapselte Phasen-Controller für Zero-Feed.

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
  Feedback-Regler.
  Berücksichtigt Korrekturen anderer Phasen für korrekte Zerlegung.
  Oszillationserkennung auf Realverbrauch.

ZeroFeedManager:
  Addiert alle Phasen-Korrekturen → finaler Batterie-Setpoint.
    Liefert ungekappte Ziel-Leistung; Batterie-Grenzen werden am Ende angewandt.
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

    target_power_w: float = 3.0
    """Ziel-Bezug in Watt – wir regeln auf diesen Wert, nicht auf den aktuellen Netzbezug."""


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


class ManagerDebugInfo(NamedTuple):
    """Zwischenwerte für Logging/Analyse."""

    feedback_output_w: float
    """Desired-Total vom Inverter-Controller."""

    ff_output_w: float
    """Summe der Feedforward-Korrekturen (A+C)."""

    raw_setpoint_w: int
    """Setpoint vor Gesamt-Oszillations-Limit."""

    osc_limit_w: float
    """Aktives Oszillations-Limit des B-Reglers."""


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
        self._last_controller_output: float = 0.0
        self._last_osc_limit: float = float("inf")

    def add_sample(self, value: float, timestamp: float) -> None:
        """Füttert Oszillationsdetektoren mit Phasen-Messwert.

        Args:
            value: Phasen-Leistung in Watt (= Realverbrauch, da kein Inverter).
            timestamp: Zeitstempel.
        """
        self._feed_oscillation_detectors(value, timestamp)

    def calculate(self, phase_power_w: list[float], target_power_w: float) -> float:
        """Berechnet Korrekturwert (Feedforward-Kompensation).

        Args:
            phase_power_w: Letzte Messwerte dieser Phase.
                Positiv = Bezug, Negativ = Einspeisung.

        Returns:
            Korrekturwert (bereits durch Oszillations-Limit begrenzt).
        """
        osc_limit = self.get_osc_limit()
        self._last_osc_limit = osc_limit

        if not phase_power_w:
            self._last_output = min(self._last_output, osc_limit)
            return self._last_output

        filtered = self.preprocessor.process(phase_power_w)
        if filtered is None:
            self._last_output = min(self._last_output, osc_limit)
            return self._last_output

        # Feedforward: Kompensiere Netzbezug oberhalb des Zielwerts
        error = filtered - target_power_w
        if abs(error) < self.settings.hysteresis_w:
            compensation = self.settings.kp_hysteresis * error
        else:
            compensation = self.settings.kp * error

        # Anti-Export-Schutz: positive Correction nie größer als aktueller
        # verfügbarer Bezug über dem Zielwert dieser Phase.
        current_phase = phase_power_w[-1]
        max_positive_correction = max(current_phase - target_power_w, 0.0)

        # Oszillations-Limit und Anti-Export-Schutz anwenden
        correction = min(compensation, osc_limit, max_positive_correction)
        self._last_controller_output = compensation
        self._last_output = correction

        return correction

    @property
    def last_output(self) -> float:
        return self._last_output

    @property
    def last_controller_output(self) -> float:
        return self._last_controller_output

    @property
    def last_osc_limit(self) -> float:
        return self._last_osc_limit


# ── InverterPhaseController (Feedback, mit Inverter) ──────────────────────────


class InverterPhaseController(_OscillationMixin):
    """Gekapselter Controller für die Phase MIT Inverter.

    Kapselt: Preprocessor, asymmetrischer P-Regler, Oszillationsdetektoren.

    Eingabe:  Grid-Leistung der B-Phase + Batteriezustand.
    Ausgabe:  Korrekturwert = Anteil dieser Phase am Batterie-Setpoint.

    Feedback: Regelt ausschließlich auf der B-Phase. Die Feedforward-
    Korrekturen der Phasen A+C gehen als Offset in den Sollwert der B-Phase
    ein, damit deren Kompensation nicht fälschlich als Fehler der B-Regelung
    interpretiert wird.

    Oszillationserkennung: Läuft auf dem geschätzten Realverbrauch der
    B-Phase. Dazu wird der Batterie-Output um den Feedforward-Anteil von
    A+C bereinigt.
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
        self._last_osc_limit: float = float("inf")
        self._last_phase_target: float = settings.target_power_w
        self._last_feedback_error: float = 0.0

    def add_sample(
        self,
        phase_power: float,
        battery_output: float,
        other_corrections_w: float,
        timestamp: float,
    ) -> None:
        """Füttert Oszillationsdetektoren mit geschätztem B-Phasen-Verbrauch.

        Args:
            phase_power: Grid-Leistung dieser Phase (beeinflusst durch Batterie).
            battery_output: Aktuelle Batterie-Ausgabe.
            other_corrections_w: Aktiver Feedforward-Anteil von A+C.
            timestamp: Zeitstempel.
        """
        real_consumption = phase_power + battery_output - other_corrections_w
        self._feed_oscillation_detectors(real_consumption, timestamp)

    def calculate(
        self,
        phase_b_grid_power_w: list[float],
        target_power_w: float,
        current_battery_output_w: float,
        other_corrections_w: float,
        settled: bool = True,
    ) -> float:
        """Berechnet Korrekturwert (Feedback nur auf Phase B).

        Der Korrekturwert repräsentiert den Anteil dieser Phase am
        gewünschten Batterie-Output.  Wenn alle Phasen-Korrekturen
        addiert werden, ergibt sich der gewünschte Total-Setpoint.

        Args:
            phase_b_grid_power_w: Letzte Messwerte der B-Phase.
            current_battery_output_w: Aktueller Batterie-Output.
            other_corrections_w: Summe der Korrekturen anderer Phasen.
            settled: True wenn Inverter am vorherigen Setpoint angekommen.
                Bei False wird Feedback eingefroren.

        Returns:
            Korrekturwert (bereits durch Oszillations-Limit begrenzt).
        """
        osc_limit = self.get_osc_limit()
        self._last_osc_limit = osc_limit
        phase_target = target_power_w - other_corrections_w
        self._last_phase_target = phase_target

        # Feedback nur aktualisieren wenn Inverter settled
        if settled and phase_b_grid_power_w:
            filtered = self.preprocessor.process(phase_b_grid_power_w)
            if filtered is not None:
                error = filtered - phase_target
                self._last_feedback_error = error

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

        # Mein Anteil = gewünschtes Total minus Feedforward-Offset von A+C
        my_correction = self._last_desired_total - other_corrections_w

        # Oszillations-Limit auf meinen Anteil anwenden
        effective = min(my_correction, osc_limit)
        self._last_output = effective

        return effective

    @property
    def last_output(self) -> float:
        return self._last_output

    @property
    def last_desired_total(self) -> float:
        return self._last_desired_total

    @property
    def last_controller_output(self) -> float:
        return self._last_desired_total

    @property
    def last_osc_limit(self) -> float:
        return self._last_osc_limit

    @property
    def last_phase_target(self) -> float:
        return self._last_phase_target

    @property
    def last_feedback_error(self) -> float:
        return self._last_feedback_error


# ── ZeroFeedManager ───────────────────────────────────────────────────────────


class ZeroFeedManager:
    """Kombiniert Phasen-Controller zu einem Batterie-Setpoint.

    1. Berechnet Feedforward-Korrekturen (Phasen ohne Inverter)
    2. Übergibt deren Summe an den Inverter-Controller
    3. Addiert alle Korrekturen
    4. Optional: Gesamt-Oszillationsdetektor als zusätzliches Limit
    5. Gibt die Ziel-Leistung zurück (Batterie-Grenzen erfolgen außerhalb)
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
        phase_b_power_w: list[float],
        phase_c_power_w: list[float],
        current_battery_output_w: float,
        battery_settled: bool = True,
    ) -> float:
        """Berechnet die ungekappte Batterie-Ziel-Leistung.

        Args:
            phase_a_power_w: Messwerte Phase A (neueste zuletzt).
            phase_b_power_w: Messwerte Phase B (neueste zuletzt).
            phase_c_power_w: Messwerte Phase C (neueste zuletzt).
            current_battery_output_w: Aktueller Batterie-Output.
            battery_settled: True wenn Inverter am Setpoint angekommen.

        Returns:
            Ziel-Leistung in Watt nach Oszillations-Limits, aber ohne Batterie-Min/Max.
        """
        # 1) Feedforward-Phasen zuerst (A + C)
        correction_a = self._phase_a.calculate(phase_a_power_w, self.settings.target_power_w)
        correction_c = self._phase_c.calculate(phase_c_power_w, self.settings.target_power_w)
        other_corrections = correction_a + correction_c

        # 2) Inverter-Phase (Feedback) – bekommt Summe der anderen
        correction_b = self._phase_b.calculate(
            phase_b_power_w,
            self.settings.target_power_w,
            current_battery_output_w,
            other_corrections,
            battery_settled,
        )

        # 3) Summe aller Korrekturen
        raw_total = correction_a + correction_b + correction_c

        self._last_setpoint = raw_total

        logger.debug(
            "Manager: ff=%.0fW  b_target=%.0fW  fb=%.0fW  settled=%s → raw=%.0fW",
            other_corrections,
            self._phase_b.last_phase_target,
            self._phase_b.last_controller_output,
            battery_settled,
            raw_total,
        )

        # Store debug info for callers that request it
        self._last_debug = ManagerDebugInfo(
            feedback_output_w=self._phase_b.last_controller_output,
            ff_output_w=other_corrections,
            raw_setpoint_w=int(round(raw_total)),
            osc_limit_w=self._phase_b.last_osc_limit,
        )

        return raw_total

    def calculate_debug(
        self,
        phase_a_power_w: list[float],
        phase_b_power_w: list[float],
        phase_c_power_w: list[float],
        current_battery_output_w: float,
        battery_settled: bool = True,
    ) -> tuple[float, ManagerDebugInfo]:
        """Wrapper: führt `calculate` aus und liefert zusätzlich Debug-Infos."""
        setpoint = self.calculate(
            phase_a_power_w,
            phase_b_power_w,
            phase_c_power_w,
            current_battery_output_w,
            battery_settled,
        )
        return setpoint, self._last_debug

    @property
    def last_feedforward_output_w(self) -> float:
        return self._phase_a.last_output + self._phase_c.last_output

"""Feedforward-Steuerung für Phasen ohne Inverter (z.B. Phase A + C in V4-Variante).

Diese Steuerung ist eine OFFENE STEUERUNG (kein Feedback-Regelkreis):
  - Beobachtet Netzbezug an einer Phase (z.B. A oder C)
  - Berechnet eine Batterie-Anfrage, um diesen Netzbezug auf 0W zu bringen
  - Kein Stabilitätsrisiko, da die Batterie an einer anderen Phase (B) hängt

Regler-Struktur (P-Steuerung mit Hysterese-Preprocessor):
  - Außerhalb Hysterese:  kp      × phase_power   (volle Kompensation, default kp=1.0)
  - Innerhalb Hysterese:  kp_hyst × phase_power   (gedämpft, default 0.3)
  - Danach: Begrenzung durch Oszillationsdetektoren (Holder + Predictor)
  - Anti-Export-Schutz:   Anfrage nie größer als aktueller Netzbezug (kein Over-Shoot)

Ausgabe: Batterie-Anfrage in Watt (immer >= 0).
Die Summe aller FF-Phasen-Anfragen bildet das variable Target für die Phase-B-Regelung.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from .oscillation_detectorv2 import (
    BaseloadHolder,
    BaseloadHolderSettings,
    BaseloadPredictor,
    BaseloadPredictorSettings,
)
from .phase_controller import PhaseSample
from .pre_processor import HysteresisPreprocessor

logger = logging.getLogger(__name__)


# ── Einstellungen ─────────────────────────────────────────────────────────────


@dataclass
class FeedforwardSteuerungSettings:
    """Einstellungen für eine Feedforward-Steuerung-Phase (z.B. A oder C)."""

    kp: float = 1.0
    """Verstärkung außerhalb der Hysterese.
    1.0 = volle Kompensation des Netzbezugs (Vorsteuerung ohne Abschwächung)."""

    kp_hysteresis: float = 0.3
    """Gedämpfte Verstärkung innerhalb der Hysterese.
    Verhindert Kleinschwingungen nahe dem Nullpunkt."""

    hysteresis_w: float = 8.0
    """Hysterese-Band in Watt.
    Innerhalb: kp_hysteresis aktiv. Außerhalb: kp aktiv."""


# ── Feedforward-Steuerung ─────────────────────────────────────────────────────


class FeedforwardSteuerung:
    """P-Steuerung für eine Phase ohne Inverter.

    Berechnet eine Batterie-Anfrage (Demand), um Netzbezug an dieser Phase
    auf 0W zu bringen. Target ist fest auf 0W gesetzt.

    Verwendet Hysterese-Preprocessor (robuste Filterung) und optionale
    Oszillationsdetektoren (Holder und/oder Predictor) als Ausgabe-Limiter.

    Die Ausgabe ist immer >= 0W: negative Werte (Einspeisung an dieser Phase)
    führen zu keiner Rücknahme der Batterie-Anfrage – das ist Aufgabe des
    Phase-B-Reglers.
    """

    def __init__(
        self,
        settings: FeedforwardSteuerungSettings,
        holder_settings: Optional[BaseloadHolderSettings] = None,
        predictor_settings: Optional[BaseloadPredictorSettings] = None,
    ):
        self.settings = settings
        self.preprocessor = HysteresisPreprocessor(hysteresis=settings.hysteresis_w)
        self.holder = BaseloadHolder(holder_settings) if holder_settings else None
        self.predictor = BaseloadPredictor(predictor_settings) if predictor_settings else None
        self._last_output: float = 0.0
        self._last_raw_output: float = 0.0
        self._last_osc_limit: float = float("inf")

    # ── Oszillation ───────────────────────────────────────────────────

    @property
    def is_oscillating(self) -> bool:
        """True wenn mindestens ein Oszillationsdetektor aktiv ist."""
        return (self.holder is not None and self.holder.is_oscillating) or (
            self.predictor is not None and self.predictor.is_oscillating
        )

    def feed_osc_samples(self, samples: Optional[list[PhaseSample]] = None) -> float:
        """Füttert Oszillationsdetektoren mit neuen Samples und gibt aktives Limit zurück.

        Nur positive Werte (Netzbezug) werden an die Detektoren übergeben –
        Einspeisung ist kein Indikator für Lastoszillation.

        Returns:
            Aktives Osc-Limit in Watt (float('inf') wenn kein Detektor aktiv).
        """
        if samples:
            for sample in samples:
                if sample.value > 0:
                    if self.holder:
                        self.holder.add_sample(sample.value, sample.timestamp)
                    if self.predictor:
                        self.predictor.add_sample(sample.value, sample.timestamp)

        limits: list[float] = []
        if self.holder and self.holder.is_oscillating:
            limits.append(self.holder.get_limit())
        if self.predictor and self.predictor.is_oscillating:
            limits.append(self.predictor.get_limit())
        return min(limits) if limits else float("inf")

    # ── Steuerungsberechnung ──────────────────────────────────────────

    def calculate(
        self,
        phase_power_w: list[float],
        osc_samples: Optional[list[PhaseSample]] = None,
    ) -> float:
        """Berechnet Batterie-Anfrage für diese Phase.

        Ablauf:
          1. Osc-Detektoren mit neuen Samples füttern → Limit ermitteln
          2. Hysterese-Preprocessor filtert Messwerte
          3. P-Steuerung: error = filtered (target=0), output = kp * error
          4. Ausgabe begrenzen: [0, min(raw, osc_limit, anti_export_cap)]

        Args:
            phase_power_w: Letzte Messwerte dieser Phase in Watt (positiv = Netzbezug,
                negativ = Einspeisung). Neueste zuletzt.
            osc_samples: Samples inkl. Zeitstempel für Oszillationsdetektoren.
                Kann von phase_power_w abweichen (z.B. kürzeres Fenster).

        Returns:
            Batterie-Anfrage in Watt. Immer >= 0, bereits durch Osc-Limit begrenzt.
        """
        osc_limit = self.feed_osc_samples(osc_samples)
        self._last_osc_limit = osc_limit

        if not phase_power_w:
            # Kein neues Sample: letzten Output nur durch Osc-Limit begrenzen
            self._last_output = min(self._last_output, osc_limit)
            return self._last_output

        filtered = self.preprocessor.process(phase_power_w)
        if filtered is None:
            self._last_output = min(self._last_output, osc_limit)
            return self._last_output

        # P-Steuerung: target = 0W → error = filtered
        error = filtered
        if abs(error) < self.settings.hysteresis_w:
            raw = self.settings.kp_hysteresis * error
        else:
            raw = self.settings.kp * error

        self._last_raw_output = raw

        # Anti-Export-Schutz: Anfrage darf aktuellen Bezug nicht übersteigen
        # Verhindert Einspeisung, wenn Last zwischen Filterung und Berechnung fällt
        current_phase = phase_power_w[-1]
        max_output = max(current_phase, 0.0)

        # Ausgabe begrenzen: [0, min(raw, osc_limit, anti_export_cap)]
        output = max(0.0, min(raw, osc_limit, max_output))
        self._last_output = output

        logger.debug(
            "FFSteuerung: phase_last=%.0fW filtered=%.1fW error=%.1fW "
            "raw=%.1fW osc_limit=%.0fW anti_exp=%.0fW → output=%.1fW",
            current_phase,
            filtered,
            error,
            raw,
            osc_limit,
            max_output,
            output,
        )
        return output

    # ── Properties ────────────────────────────────────────────────────

    @property
    def last_output(self) -> float:
        """Letzter Ausgabewert (bereits begrenzt)."""
        return self._last_output

    @property
    def last_raw_output(self) -> float:
        """Letzter ungekappter P-Steuerungsoutput (vor Osc-Limit + Anti-Export)."""
        return self._last_raw_output

    @property
    def last_osc_limit(self) -> float:
        """Aktives Oszillations-Limit (inf wenn kein Detektor aktiv)."""
        return self._last_osc_limit

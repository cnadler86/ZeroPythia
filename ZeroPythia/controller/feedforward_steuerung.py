"""Feedforward control for phases without inverter (e.g. phases A + C).

This is an OPEN-LOOP CONTROLLER (no feedback):
  - Observes grid draw on a phase (e.g. A or C)
  - Computes a battery request to bring that grid draw to 0 W
  - No stability risk, because the battery is connected to a different phase (B)

Controller structure (P-control with hysteresis preprocessor):
  - Outside hysteresis: kp      × phase_power   (full compensation, default kp=1.0)
  - Inside  hysteresis: kp_hyst × phase_power   (damped, default 0.3)
  - Followed by clamping via oscillation detectors (Holder + Predictor)
  - Anti-export guard:  request never exceeds current grid draw (no over-shoot)

Output: battery request in watts (always >= 0).
The sum of all FF-phase requests forms the variable target for phase-B control.
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
from .phase_controller import PhaseSample, _OscillationMixin
from .pre_processor import HysteresisPreprocessor

logger = logging.getLogger(__name__)


# ── Settings ──────────────────────────────────────────────────────────────────


@dataclass
class FeedforwardSteuerungSettings:
    """Settings for a feedforward-control phase (e.g. A or C)."""

    kp: float = 1.0
    """Gain outside the hysteresis band.
    1.0 = full compensation of grid draw (pure feedforward, no attenuation)."""

    kp_hysteresis: float = 0.3
    """Damped gain inside the hysteresis band.
    Prevents small oscillations near the zero-point."""

    hysteresis_w: float = 8.0
    """Hysteresis band width in watts.
    Inside: kp_hysteresis active. Outside: kp active."""


# ── Feedforward control ───────────────────────────────────────────────────────


class FeedforwardSteuerung(_OscillationMixin):
    """P-controller for a phase without inverter.

    Computes a battery demand to bring the grid draw of this phase to 0 W.
    The target is fixed at 0 W.

    Uses a hysteresis preprocessor (robust filtering) and optional
    oscillation detectors (Holder and/or Predictor) as output limiters.

    Output is always >= 0 W: negative values (feed-in on this phase)
    do not reduce the battery request – that is the responsibility of
    the phase-B controller.
    """

    def __init__(
        self,
        settings: FeedforwardSteuerungSettings,
        holder_settings: Optional[BaseloadHolderSettings] = None,
        predictor_settings: Optional[BaseloadPredictorSettings] = None,
        phase_label: str | None = None,
    ):
        self.settings = settings
        self._phase_label = phase_label or "?"
        self._osc_log_label = f"FF phase={self._phase_label}"
        self.preprocessor = HysteresisPreprocessor(hysteresis=settings.hysteresis_w)
        self.holder = (
            BaseloadHolder(holder_settings, phase_label=self._phase_label)
            if holder_settings
            else None
        )
        self.predictor = (
            BaseloadPredictor(predictor_settings, phase_label=self._phase_label)
            if predictor_settings
            else None
        )
        self._last_output: float = 0.0
        self._last_raw_output: float = 0.0
        self._last_osc_limit: float = float("inf")

    # ── Control calculation ───────────────────────────────────────────

    def calculate(
        self,
        phase_power_w: list[float],
        osc_samples: Optional[list[PhaseSample]] = None,
    ) -> float:
        """Compute the battery demand for this phase.

        Steps:
          1. Feed oscillation detectors with new samples → determine limit
          2. Hysteresis preprocessor filters measurements
          3. P-control: error = filtered (target=0), output = kp * error
          4. Clamp output: [0, min(raw, osc_limit, anti_export_cap)]

        Args:
            phase_power_w: Latest samples for this phase in watts (positive = grid
                draw, negative = feed-in). Newest last.
            osc_samples: Samples with timestamps for oscillation detectors.
                May differ from phase_power_w (e.g. shorter window).

        Returns:
            Battery demand in watts. Always >= 0, already capped by osc limit.
        """
        osc_limit = self.get_osc_limit(osc_samples)
        self._last_osc_limit = osc_limit

        if not phase_power_w:
            # No new sample: only clamp last output by osc limit
            self._last_output = min(self._last_output, osc_limit)
            return self._last_output

        filtered = self.preprocessor.process(phase_power_w)
        if filtered is None:
            self._last_output = min(self._last_output, osc_limit)
            return self._last_output

        # P-control: target = 0 W → error = filtered
        error = filtered
        if abs(error) < self.settings.hysteresis_w:
            raw = self.settings.kp_hysteresis * error
        else:
            raw = self.settings.kp * error

        self._last_raw_output = raw

        # Anti-export guard: request must not exceed current draw.
        # Prevents feed-in if the load drops between filtering and calculation.
        current_phase = phase_power_w[-1]
        max_output = max(current_phase, 0.0)

        # Clamp output: [0, min(raw, osc_limit, anti_export_cap)]
        output = max(0.0, min(raw, osc_limit, max_output))
        self._last_output = output

        logger.debug(
            "FFSteuerung[%s]: phase_last=%.0fW filtered=%.1fW error=%.1fW "
            "raw=%.1f W  osc_limit=%.0f W  anti_exp=%.0f W  → output=%.1f W",
            self._phase_label,
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
        """Last output value (already clamped)."""
        return self._last_output

    @property
    def last_raw_output(self) -> float:
        """Last uncapped P-control output (before osc limit + anti-export)."""
        return self._last_raw_output

    @property
    def last_osc_limit(self) -> float:
        """Active oscillation limit (inf when no detector is active)."""
        return self._last_osc_limit

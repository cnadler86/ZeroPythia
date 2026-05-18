"""Per-phase controllers for zero-feed-in regulation.

Architecture - each phase controller encapsulates:
  1. Preprocessor  (hysteresis-based sample filtering)
  2. P-controller  (feedforward or feedback)
  3. Oscillation detectors (Holder + Predictor)

Input:  per-phase power samples from the energy meter
Output: correction value (desired battery compensation for this phase)

PhaseController (phases WITHOUT inverter, e.g. A+C):
  Pure feedforward.  Observes grid draw → requests battery compensation.
  No stability risk, because the battery does not affect this phase.

InverterPhaseController (phase WITH inverter, e.g. B):
  Feedback regulator.
  Takes corrections from other phases into account for correct decomposition.
  Oscillation detection on real consumption.
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
from .pre_processor import HysteresisPreprocessor

logger = logging.getLogger(__name__)


# ── Settings ──────────────────────────────────────────────────────────────────


@dataclass
class InverterPhaseControllerSettings:
    """Settings for the feedback phase controller (phase with inverter)."""

    kp_draw: float = 0.95
    """Gain for grid draw (conservative)"""

    kp_feed_in: float = 1.05
    """Gain for feed-in (aggressive)"""

    hysteresis_w: float = 10.0
    """Hysteresis band in watts"""

    kp_hysteresis: float = 0.3
    """Damped Kp inside the hysteresis band"""

    target_power_w: float = 1.0
    """Target draw in watts – regulate to this value, not to current grid draw."""

    feedback_enabled: bool = True
    """False = pure feedforward mode (phase B returns correction=0, total=A+C).
    Useful for tests and when phase B has no independent load."""


@dataclass(frozen=True)
class PhaseSample:
    """Timestamp and value for a phase sample."""

    timestamp: float
    value: float


# ── Oscillation helpers ───────────────────────────────────────────────────────


class _OscillationMixin:
    """Shared oscillation-detection logic for both controller types."""

    holder: Optional[BaseloadHolder]
    predictor: Optional[BaseloadPredictor]

    @property
    def is_oscillating(self) -> bool:
        return (self.holder is not None and self.holder.is_oscillating) or (
            self.predictor is not None and self.predictor.is_oscillating
        )

    def get_osc_limit(self, samples: Optional[list[PhaseSample]] = None) -> float:
        if samples:
            passed, filtered = 0, 0
            for sample in samples:
                if sample.value > 0:
                    passed += 1
                    if self.holder:
                        self.holder.add_sample(sample.value, sample.timestamp)
                    if self.predictor:
                        self.predictor.add_sample(sample.value, sample.timestamp)
                else:
                    filtered += 1
            if filtered > 0:
                logger.debug(
                    "get_osc_limit: %d sample(s) filtered (<=0), %d passed",
                    filtered,
                    passed,
                )

        limits: list[float] = []
        if self.holder and self.holder.is_oscillating:
            limits.append(self.holder.get_limit())
        if self.predictor and self.predictor.is_oscillating:
            limits.append(self.predictor.get_limit())
        if limits:
            logger.debug("get_osc_limit: active limits=%s → %.0fW", limits, min(limits))
        return min(limits) if limits else float("inf")


# ── InverterPhaseController (feedback, with inverter) ─────────────────────────


class InverterPhaseController(_OscillationMixin):
    """Controller for the phase WITH inverter.

    Encapsulates: preprocessor, asymmetric P-regulator, oscillation detectors.

    Input:  grid power of phase B + battery state.
    Output: correction = this phase's share of the battery setpoint.

    Feedback: regulates exclusively on phase B.  Feedforward corrections
    from phases A+C enter as an offset into the phase-B target so that
    their compensation is not misinterpreted as a phase-B control error.

    Oscillation detection: operates on the estimated real consumption of
    phase B, which is the B-phase grid power corrected for A+C feedforward.
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

    def calculate(
        self,
        phase_b_grid_power_w: list[float],
        target_power_w: float,
        current_battery_output_w: float,
        other_corrections_w: float,
        settled: bool = True,
        osc_samples: Optional[list[PhaseSample]] = None,
    ) -> float:
        """Compute the correction value (feedback on phase B only).

        The correction represents this phase's share of the desired battery
        output.  Summing all phase corrections gives the desired total setpoint.

        Args:
            phase_b_grid_power_w: Latest samples for phase B.
            current_battery_output_w: Current battery output.
            other_corrections_w: Sum of corrections from other phases.
            settled: True when the inverter has reached the previous setpoint.
                When False, feedback is frozen.

        Returns:
            Correction value (already capped by oscillation limit).
        """
        osc_limit = self.get_osc_limit(osc_samples)
        self._last_osc_limit = osc_limit
        phase_target = target_power_w - other_corrections_w
        self._last_phase_target = phase_target

        # Wenn Feedback deaktiviert: immer 0 zurückgeben (reiner FF-Modus)
        if not self.settings.feedback_enabled:
            logger.debug(
                "InverterPhaseController: feedback_enabled=False – B-correction=0"
                " (ff_sum=%.0f W used as total)",
                other_corrections_w,
            )
            self._last_output = 0.0
            return 0.0

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

                new_desired = current_battery_output_w + correction
                logger.debug(
                    "InverterPhaseController [settled]: phase_b=%.0fW filtered=%.1fW"
                    " phase_target=%.1fW error=%.1fW correction=%.1fW"
                    " batt_base=%.0fW → desired_total=%.0fW",
                    phase_b_grid_power_w[-1] if phase_b_grid_power_w else float("nan"),
                    filtered,
                    phase_target,
                    error,
                    correction,
                    current_battery_output_w,
                    new_desired,
                )
                self._last_desired_total = new_desired
        else:
            if not settled:
                logger.debug(
                    "InverterPhaseController [!settled]: feedback frozen"
                    " at desired_total=%.0f W  phase_target=%.1f W  ff_sum=%.0f W",
                    self._last_desired_total,
                    phase_target,
                    other_corrections_w,
                )
            elif not phase_b_grid_power_w:
                logger.debug("InverterPhaseController: no phase-B samples")

        # My share = desired total minus feedforward offset from A+C
        my_correction = self._last_desired_total - other_corrections_w

        # Apply oscillation limit to my share
        effective = min(my_correction, osc_limit)
        self._last_output = effective

        logger.debug(
            "InverterPhaseController: desired_total=%.0fW  ff=%.0fW"
            " → my_corr=%.0fW  osc_limit=%.0fW  effective=%.0fW",
            self._last_desired_total,
            other_corrections_w,
            my_correction,
            osc_limit,
            effective,
        )
        return effective

    def apply_effective_total(self, effective_total_w: float, other_corrections_w: float) -> None:
        """Align internal controller state to the actually applied battery setpoint.

        This prevents windup when callers request a value above hardware limits
        and the battery clamps to a lower effective setpoint.
        """
        self._last_desired_total = float(effective_total_w)
        my_correction = self._last_desired_total - other_corrections_w
        self._last_output = min(my_correction, self._last_osc_limit)

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

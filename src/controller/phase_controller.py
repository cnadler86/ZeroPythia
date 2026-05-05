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

ZeroFeedManager:
  Adds all phase corrections → final battery setpoint.
  Delivers uncapped target power; battery limits are applied afterwards.
  Optional global oscillation detector as safety net.
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


# ── Settings ──────────────────────────────────────────────────────────────────


@dataclass
class PhaseControllerSettings:
    """Settings for the feedforward phase controller (phases without inverter)."""

    kp: float = 1.0
    """Gain: 1.0 = full compensation of grid draw"""

    hysteresis_w: float = 8.0
    """Hysteresis band in watts – damped control within this zone"""

    kp_hysteresis: float = 0.3
    """Damped Kp inside the hysteresis band"""


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


@dataclass
class ZeroFeedManagerSettings:
    """Settings for the ZeroFeedManager."""

    min_output_w: int = 20
    """Minimum battery output (hardware limit)"""

    max_output_w: int = 800
    """Maximum battery output (hardware limit)"""

    target_power_w: float = 1.0
    """Target draw in watts – only compensate what exceeds this value.
    Positive values reduce feed-in at the cost of a small permanent draw."""


class ManagerDebugInfo(NamedTuple):
    """Intermediate values for logging/analysis."""

    feedback_output_w: float
    """Desired-total from the inverter controller."""

    ff_output_w: float
    """Sum of feedforward corrections (A+C)."""

    raw_setpoint_w: int
    """Setpoint before global oscillation limit."""

    osc_limit_w: float
    """Active oscillation limit of the B-regulator."""


@dataclass(frozen=True)
class PhaseSample:
    """Timestamp and value for a phase sample."""

    timestamp: float
    value: float


def _sample_values(samples: Optional[list[PhaseSample]]) -> list[float]:
    if not samples:
        return []
    return [sample.value for sample in samples]


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


# ── PhaseController (feedforward, without inverter) ──────────────────────────


class PhaseController(_OscillationMixin):
    """Controller for a phase WITHOUT inverter.

    Encapsulates: preprocessor, P-regulator, oscillation detectors.

    Input:  phase power from the energy meter (positive = grid draw).
    Output: correction = desired battery compensation for this phase.

    Feedforward: no stability risk, because the battery does not affect
    this phase (battery is connected to a different phase).
    """

    def __init__(
        self,
        settings: PhaseControllerSettings,
        holder_settings: Optional[BaseloadHolderSettings] = None,
        predictor_settings: Optional[BaseloadPredictorSettings] = None,
    ):
        self.settings = settings
        self.preprocessor = HysteresisPreprocessor(hysteresis=settings.hysteresis_w)
        self.holder = BaseloadHolder(holder_settings) if holder_settings else None
        self.predictor = BaseloadPredictor(predictor_settings) if predictor_settings else None
        self._last_output: float = 0.0
        self._last_controller_output: float = 0.0
        self._last_osc_limit: float = float("inf")

    def calculate(
        self,
        phase_power_w: list[float],
        target_power_w: float,
        osc_samples: Optional[list[PhaseSample]] = None,
    ) -> float:
        """Compute the correction value (feedforward compensation).

        Args:
            phase_power_w: Latest samples for this phase.
                Positive = grid draw, negative = feed-in.

        Returns:
            Correction value (already capped by oscillation limit).
        """
        osc_limit = self.get_osc_limit(osc_samples)
        self._last_osc_limit = osc_limit

        if not phase_power_w:
            self._last_output = min(self._last_output, osc_limit)
            return self._last_output

        filtered = self.preprocessor.process(phase_power_w)
        if filtered is None:
            self._last_output = min(self._last_output, osc_limit)
            return self._last_output

        # Feedforward: compensate grid draw above target
        error = filtered - target_power_w
        if abs(error) < self.settings.hysteresis_w:
            compensation = self.settings.kp_hysteresis * error
        else:
            compensation = self.settings.kp * error

        # Anti-export guard: positive correction must not exceed current
        # available draw above the target for this phase.
        current_phase = phase_power_w[-1]
        max_positive_correction = max(current_phase - target_power_w, 0.0)

        # Apply oscillation limit and anti-export guard
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


# ── ZeroFeedManager ───────────────────────────────────────────────────────────


class ZeroFeedManager:
    """Combines phase controllers into a single battery setpoint.

    1. Computes feedforward corrections (phases without inverter)
    2. Passes their sum to the inverter controller
    3. Sums all corrections
    4. Optional: global oscillation detector as additional limit
    5. Returns target power (battery limits are applied outside)
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

        # Optional global detector (safety net)
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

    @property
    def total_is_oscillating(self) -> bool:
        return (self._total_holder is not None and self._total_holder.is_oscillating) or (
            self._total_predictor is not None and self._total_predictor.is_oscillating
        )

    def get_total_osc_limit(self, samples: Optional[list[PhaseSample]] = None) -> float:
        if samples:
            for sample in samples:
                if sample.value > 0:
                    if self._total_holder:
                        self._total_holder.add_sample(sample.value, sample.timestamp)
                    if self._total_predictor:
                        self._total_predictor.add_sample(sample.value, sample.timestamp)

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

    # ── Control ───────────────────────────────────────────────────────

    def calculate(
        self,
        phase_a_samples: Optional[list[PhaseSample]],
        phase_b_samples: Optional[list[PhaseSample]],
        phase_c_samples: Optional[list[PhaseSample]],
        current_battery_output_w: float,
        battery_settled: bool = True,
        phase_battery_output_samples_w: Optional[list[float]] = None,
        total_osc_samples: Optional[list[PhaseSample]] = None,
    ) -> float:
        """Compute the uncapped battery target power.

        Args:
            phase_a_samples: Phase-A samples with timestamps (newest last).
            phase_b_samples: Phase-B samples with timestamps (newest last).
            phase_c_samples: Phase-C samples with timestamps (newest last).
            current_battery_output_w: Current battery output.
            battery_settled: True when the inverter has reached the setpoint.

        Returns:
            Target power in watts after oscillation limits, without battery min/max.
        """
        phase_a_power_w = _sample_values(phase_a_samples)
        phase_b_power_w = _sample_values(phase_b_samples)
        phase_c_power_w = _sample_values(phase_c_samples)

        # 1) Feedforward phases first (A + C)
        correction_a = self._phase_a.calculate(
            phase_a_power_w,
            self.settings.target_power_w,
            osc_samples=phase_a_samples,
        )
        correction_c = self._phase_c.calculate(
            phase_c_power_w,
            self.settings.target_power_w,
            osc_samples=phase_c_samples,
        )
        other_corrections = correction_a + correction_c
        logger.debug(
            "Manager FF: A=%.0fW (raw=%.0fW osc=%.0fW)  C=%.0fW (raw=%.0fW osc=%.0fW)  sum=%.0fW",
            correction_a,
            self._phase_a.last_controller_output,
            self._phase_a.last_osc_limit,
            correction_c,
            self._phase_c.last_controller_output,
            self._phase_c.last_osc_limit,
            other_corrections,
        )

        phase_b_real_consumption_samples: Optional[list[PhaseSample]] = None
        if phase_b_samples and phase_battery_output_samples_w:
            n = min(len(phase_b_samples), len(phase_battery_output_samples_w))
            phase_b_real_consumption_samples = [
                PhaseSample(
                    timestamp=phase_b_samples[i].timestamp,
                    value=phase_b_samples[i].value
                    + phase_battery_output_samples_w[i]
                    - other_corrections,
                )
                for i in range(n)
            ]

        # 2) Inverter phase (feedback) – receives sum of the other corrections
        correction_b = self._phase_b.calculate(
            phase_b_power_w,
            self.settings.target_power_w,
            current_battery_output_w,
            other_corrections,
            battery_settled,
            osc_samples=phase_b_real_consumption_samples,
        )

        # 3) Sum of all corrections
        raw_total = correction_a + correction_b + correction_c

        self._last_setpoint = raw_total

        logger.debug(
            "Manager summary: ff=%.0fW  b_target=%.0fW  fb=%.0fW  settled=%s  corr_a=%.0fW corr_b=%.0fW corr_c=%.0fW -> raw=%.0fW",
            other_corrections,
            self._phase_b.last_phase_target,
            self._phase_b.last_controller_output,
            battery_settled,
            correction_a,
            correction_b,
            correction_c,
            raw_total,
        )

        # Feed the global oscillation detector on-the-fly with current samples.
        self.get_total_osc_limit(total_osc_samples)

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
        phase_a_samples: Optional[list[PhaseSample]],
        phase_b_samples: Optional[list[PhaseSample]],
        phase_c_samples: Optional[list[PhaseSample]],
        current_battery_output_w: float,
        battery_settled: bool = True,
        phase_battery_output_samples_w: Optional[list[float]] = None,
        total_osc_samples: Optional[list[PhaseSample]] = None,
    ) -> tuple[float, ManagerDebugInfo]:
        """Wrapper: runs `calculate` and additionally returns debug info."""
        setpoint = self.calculate(
            phase_a_samples,
            phase_b_samples,
            phase_c_samples,
            current_battery_output_w,
            battery_settled,
            phase_battery_output_samples_w,
            total_osc_samples,
        )
        return setpoint, self._last_debug

    @property
    def last_feedforward_output_w(self) -> float:
        return self._phase_a.last_output + self._phase_c.last_output

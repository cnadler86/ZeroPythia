"""Unit tests for BaseloadPredictor.get_limit().

Verifies the correct rising-flank / prediction-window behaviour:

- At the **rising-edge sample** the predictor must NOT apply any limit so that
  normal regulation can fully react to the load spike.
- During the **free high phase** (before the reaction window) no limit is applied.
- Within the **reaction window** (reaction_time seconds before the predicted
  falling edge) the predictor reduces the allowed correction to base_load.
- During the **low phase** the predictor always returns base_load.
- Regression: even when min_high_time <= reaction_time the rising-edge sample
  itself is never limited (the original bug: "immer das Limit gehalten").
"""

from __future__ import annotations

import pytest

from ZeroPythia.controller.oscillation_detectorv2 import BaseloadPredictor, BaseloadPredictorSettings

# ── Signal constants ──────────────────────────────────────────────────────────

BASE_W: float = 100.0       # low-phase load
HIGH_W: float = 1000.0      # high-phase load
PERIOD_S: float = 30.0      # oscillation period
HIGH_DURATION_S: float = 10.0  # duration of the high phase within one period
LOW_DURATION_S: float = PERIOD_S - HIGH_DURATION_S  # 20 s

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_predictor(reaction_time: float = 4.0) -> BaseloadPredictor:
    return BaseloadPredictor(
        BaseloadPredictorSettings(
            threshold=100.0,
            min_period=20.0,
            max_period=200.0,
            period_variance=0.5,
            time_threshold=2.0,
            min_rising_count=3,
            base_load_window=2,
            reaction_time=reaction_time,
        )
    )


def _feed_cycle(
    predictor: BaseloadPredictor,
    t_rising: float,
    high_w: float = HIGH_W,
    base_w: float = BASE_W,
    high_duration: float = HIGH_DURATION_S,
    period: float = PERIOD_S,
    samples_per_phase: int = 5,
) -> None:
    """Feed one full period (rising flank → high plateau → falling flank → low plateau)."""
    low_duration = period - high_duration

    # High phase
    for i in range(samples_per_phase):
        t = t_rising + high_duration * i / samples_per_phase
        predictor.add_sample(high_w, t)

    # Falling edge + low phase
    for i in range(samples_per_phase):
        t = t_rising + high_duration + low_duration * i / samples_per_phase
        predictor.add_sample(base_w, t)


def _setup_oscillation(predictor: BaseloadPredictor) -> None:
    """Feed 3 full cycles so the predictor detects oscillation (min_rising_count=3).

    After this call:
    - predictor.is_oscillating == True
    - _falling_times contains at least the cycle-2 falling edge
    - phase is 'low', last timestamp ≈ (3 * PERIOD_S - small delta)
    """
    for cycle in range(3):
        _feed_cycle(predictor, t_rising=cycle * PERIOD_S)


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestBaseloadPredictorGetLimit:

    # ── Precondition ─────────────────────────────────────────────────────────

    def test_oscillation_detected_after_three_cycles(self) -> None:
        predictor = _make_predictor()
        _setup_oscillation(predictor)
        assert predictor.is_oscillating

    # ── Rising edge: no limit ─────────────────────────────────────────────────

    def test_no_limit_at_rising_edge_after_oscillation_detected(self) -> None:
        """At the rising-edge sample the predictor must return inf (no limit)."""
        predictor = _make_predictor(reaction_time=4.0)
        _setup_oscillation(predictor)

        # 4th rising edge
        t_rise4 = 3 * PERIOD_S  # t = 90 s
        predictor.add_sample(HIGH_W, t_rise4)

        assert predictor._phase == "high"
        limit = predictor.get_limit()
        assert limit == float("inf"), (
            f"Expected no limit (inf) at the rising-edge sample, got {limit}"
        )

    # ── Free high phase: no limit ─────────────────────────────────────────────

    def test_no_limit_in_free_high_phase(self) -> None:
        """During the high phase, before the reaction window, the limit must be inf."""
        predictor = _make_predictor(reaction_time=4.0)
        _setup_oscillation(predictor)

        # 4th rising edge at t=90, reaction window starts at t=90+10-4=96
        t_rise4 = 3 * PERIOD_S
        predictor.add_sample(HIGH_W, t_rise4)          # t=90 – rising edge
        predictor.add_sample(HIGH_W, t_rise4 + 2.0)   # t=92 – 4 s before window

        assert predictor._phase == "high"
        limit = predictor.get_limit()
        assert limit == float("inf"), (
            f"Expected no limit at t=92 (reaction window starts at 96), got {limit}"
        )

    # ── Reaction window: base_load limit ─────────────────────────────────────

    def test_limit_applied_at_start_of_reaction_window(self) -> None:
        """At expected_falling - reaction_time the predictor must return base_load."""
        predictor = _make_predictor(reaction_time=4.0)
        _setup_oscillation(predictor)

        t_rise4 = 3 * PERIOD_S                              # t=90
        # expected_falling = 90 + 10 = 100; window start = 100 - 4 = 96
        t_window_start = t_rise4 + HIGH_DURATION_S - 4.0    # t=96
        predictor.add_sample(HIGH_W, t_rise4)               # t=90 rising
        predictor.add_sample(HIGH_W, t_window_start + 0.1)  # t=96.1 – just inside window

        assert predictor._phase == "high"
        limit = predictor.get_limit()
        assert limit < float("inf"), (
            f"Expected base_load limit inside reaction window, got {limit}"
        )
        # The limit should be approximately BASE_W (100 W), not the full high value
        assert limit <= BASE_W * 2, (
            f"Limit {limit} W is unexpectedly large; expected ≈ {BASE_W} W (base load)"
        )

    def test_limit_just_before_reaction_window_is_inf(self) -> None:
        """One sample before the reaction window: still no limit."""
        predictor = _make_predictor(reaction_time=4.0)
        _setup_oscillation(predictor)

        t_rise4 = 3 * PERIOD_S
        t_just_before = t_rise4 + HIGH_DURATION_S - 4.0 - 0.1  # t=95.9
        predictor.add_sample(HIGH_W, t_rise4)
        predictor.add_sample(HIGH_W, t_just_before)

        assert predictor._phase == "high"
        assert predictor.get_limit() == float("inf")

    # ── Low phase: base_load limit ────────────────────────────────────────────

    def test_limit_during_low_phase(self) -> None:
        """During the low phase the predictor must always return base_load."""
        predictor = _make_predictor(reaction_time=4.0)
        _setup_oscillation(predictor)

        # Trigger a 4th rising edge then a falling edge
        t_rise4 = 3 * PERIOD_S
        t_fall4 = t_rise4 + HIGH_DURATION_S
        predictor.add_sample(HIGH_W, t_rise4)
        predictor.add_sample(BASE_W, t_fall4)

        assert predictor._phase == "low"
        limit = predictor.get_limit()
        assert limit < float("inf"), (
            f"Expected base_load limit during low phase, got {limit}"
        )
        assert limit <= BASE_W * 2

    # ── Regression: min_high_time <= reaction_time ────────────────────────────

    def test_no_limit_at_rising_edge_when_min_high_time_less_than_reaction_time(
        self,
    ) -> None:
        """Regression for the original bug: 'predictor immer das Limit gehalten'.

        When min_high_time (3 s) < reaction_time (5 s) the OLD code applied
        base_load immediately at the rising edge because
            t_rising >= expected_falling - reaction_time
            t_rising >= (t_rising + 3) - 5 = t_rising - 2   →  always True.

        The NEW code must NOT limit at the exact rising-edge sample.
        """
        SHORT_HIGH = 3.0
        REACTION = 5.0  # > SHORT_HIGH  →  old bug triggered here
        predictor = _make_predictor(reaction_time=REACTION)

        for cycle in range(3):
            _feed_cycle(
                predictor,
                t_rising=cycle * PERIOD_S,
                high_duration=SHORT_HIGH,
                period=PERIOD_S,
            )

        assert predictor.is_oscillating

        # 4th rising edge
        t_rise4 = 3 * PERIOD_S
        predictor.add_sample(HIGH_W, t_rise4)

        assert predictor._phase == "high"
        limit = predictor.get_limit()
        assert limit == float("inf"), (
            f"Bug regression: with min_high_time={SHORT_HIGH} s < reaction_time={REACTION} s "
            f"the rising-edge sample must still return inf, got {limit}"
        )


# ── Fundamental backfill tests ─────────────────────────────────────────────────────────────────


class TestBaseloadPredictorFundamental:
    """Tests for the first detection cycle using backfilled falling edges.

    Signal design (same constants as above):
      - Rising edges at t=0, 30, 60  (3rd edge triggers detection, min_rising_count=3)
      - Falling edges backfilled from edge detector: t=10, 40
      - min_high_time = 10 s  →  predicted_falling after 3rd rise = 60+10 = 70
      - reaction_time = 4.0 s →  reaction window = [66, 70)

    These tests verify that – after the backfill fix – the predictor can react
    already in the FIRST high phase after detection, not only from the second one.
    """

    REACTION_TIME: float = 4.0

    def _build(self) -> BaseloadPredictor:
        """Feed 2 full cycles then the single rising-edge sample that triggers detection."""
        pred = _make_predictor(reaction_time=self.REACTION_TIME)
        _feed_cycle(pred, t_rising=0.0)
        _feed_cycle(pred, t_rising=PERIOD_S)
        # 3rd rising edge only – triggers oscillation detection + backfill
        pred.add_sample(HIGH_W, 2 * PERIOD_S)
        return pred

    # ── Precondition ─────────────────────────────────────────────────────────

    def test_is_oscillating_after_third_rising_edge(self) -> None:
        pred = self._build()
        assert pred.is_oscillating

    def test_falling_times_backfilled(self) -> None:
        """After detection, _falling_times must contain the pre-detection falling edges."""
        pred = self._build()
        assert len(pred._falling_times) >= 2, (
            f"Expected at least 2 backfilled falling edges, got {pred._falling_times}"
        )

    # ── 3rd rising edge: no limit ─────────────────────────────────────────────

    def test_limit_inf_at_third_rising_edge(self) -> None:
        """At the exact moment of the 3rd rising edge, limit must be inf."""
        pred = self._build()
        # _current_timestamp == last_rising → rising-edge guard must return inf
        assert pred._phase == "high"
        assert pred.get_limit() == float("inf"), (
            "Expected inf at the 3rd rising-edge sample (first detection moment)"
        )

    # ── Before reaction window: no limit ─────────────────────────────────────

    def test_limit_inf_before_reaction_window(self) -> None:
        """Sample at predicted_falling - reaction_time - 1.5 s → still inf."""
        pred = self._build()
        # predicted_falling = 2*30 + 10 = 70, window_start = 70 - 4 = 66
        # t = 70 - 4 - 1.5 = 64.5
        t_before = 2 * PERIOD_S + HIGH_DURATION_S - self.REACTION_TIME - 1.5  # 64.5
        pred.add_sample(HIGH_W, t_before)
        assert pred._phase == "high"
        assert pred.get_limit() == float("inf"), (
            f"Expected inf at t={t_before} (reaction window starts at 66)"
        )

    # ── Inside reaction window: base_load limit ───────────────────────────────

    def test_limit_base_load_in_reaction_window(self) -> None:
        """Sample at predicted_falling - reaction_time + 0.5 s → base_load."""
        pred = self._build()
        # predicted_falling = 70, window_start = 66
        # t = 70 - 4 + 0.5 = 66.5
        t_inside = 2 * PERIOD_S + HIGH_DURATION_S - self.REACTION_TIME + 0.5  # 66.5
        pred.add_sample(HIGH_W, t_inside)
        assert pred._phase == "high"
        limit = pred.get_limit()
        assert limit < float("inf"), (
            f"Expected base_load limit at t={t_inside} (inside reaction window), got inf"
        )
        assert limit == pytest.approx(BASE_W, rel=0.5), (
            f"Expected limit ≈ {BASE_W} W (base load), got {limit} W"
        )

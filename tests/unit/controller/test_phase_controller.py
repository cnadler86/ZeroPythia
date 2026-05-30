"""Unit tests for InverterPhaseController (feedback / regulation phase).

Covers:
- Basic feedback loop: error → correction
- Asymmetric gains (kp_draw vs. kp_feed_in)
- Hysteresis band damping
- Settled / not-settled gating
- Feedforward offset (other_corrections_w) decomposition
- feedback_enabled=False (pure FF mode)
- apply_effective_total anti-windup
"""

from __future__ import annotations

import pytest

from ZeroPythia.controller.phase_controller import (
    InverterPhaseController,
    InverterPhaseControllerSettings,
    PhaseSample,
)


def _make_fb(
    *,
    kp_draw: float = 0.9,
    kp_feed_in: float = 1.05,
    kp_hysteresis: float = 0.3,
    hysteresis_w: float = 10.0,
    target_power_w: float = 3.0,
    feedback_enabled: bool = True,
) -> InverterPhaseController:
    return InverterPhaseController(
        settings=InverterPhaseControllerSettings(
            kp_draw=kp_draw,
            kp_feed_in=kp_feed_in,
            kp_hysteresis=kp_hysteresis,
            hysteresis_w=hysteresis_w,
            target_power_w=target_power_w,
            feedback_enabled=feedback_enabled,
        ),
        phase_label="B",
    )


class TestFeedbackBasic:

    def test_positive_error_increases_output(self) -> None:
        """Grid draw above target → positive correction (increase battery output)."""
        fb = _make_fb(target_power_w=3.0)
        correction = fb.calculate(
            phase_b_grid_power_w=[100.0],
            target_power_w=3.0,
            current_battery_output_w=50.0,
            other_corrections_w=0.0,
            settled=True,
        )
        # error = filtered(100) - target(3) = 97 → positive correction
        assert fb.last_desired_total > 50.0

    def test_negative_error_decreases_output(self) -> None:
        """Grid draw below target (feed-in) → negative correction."""
        fb = _make_fb(target_power_w=3.0)
        correction = fb.calculate(
            phase_b_grid_power_w=[-50.0],
            target_power_w=3.0,
            current_battery_output_w=200.0,
            other_corrections_w=0.0,
            settled=True,
        )
        # error = filtered(-50) - 3 = -53 → negative correction
        assert fb.last_desired_total < 200.0


class TestFeedbackAsymmetricGains:

    def test_draw_uses_kp_draw(self) -> None:
        fb = _make_fb(kp_draw=0.5, kp_feed_in=2.0, hysteresis_w=5.0, target_power_w=0.0)
        fb.calculate(
            phase_b_grid_power_w=[100.0],
            target_power_w=0.0,
            current_battery_output_w=0.0,
            other_corrections_w=0.0,
            settled=True,
        )
        # error = 100 > 0 (draw) → kp_draw=0.5, correction = 0.5 * 100 = 50
        assert fb.last_desired_total == pytest.approx(50.0)

    def test_feedin_uses_kp_feed_in(self) -> None:
        fb = _make_fb(kp_draw=0.5, kp_feed_in=2.0, hysteresis_w=5.0, target_power_w=0.0)
        fb.calculate(
            phase_b_grid_power_w=[-100.0],
            target_power_w=0.0,
            current_battery_output_w=200.0,
            other_corrections_w=0.0,
            settled=True,
        )
        # error = -100 < 0 (feed-in) → kp_feed_in=2.0, correction = 2.0 * (-100) = -200
        assert fb.last_desired_total == pytest.approx(0.0)


class TestFeedbackHysteresis:

    def test_small_error_uses_damped_gain(self) -> None:
        fb = _make_fb(kp_draw=1.0, kp_hysteresis=0.3, hysteresis_w=10.0, target_power_w=0.0)
        fb.calculate(
            phase_b_grid_power_w=[5.0],
            target_power_w=0.0,
            current_battery_output_w=0.0,
            other_corrections_w=0.0,
            settled=True,
        )
        # error = 5 < hysteresis(10) → kp_hysteresis * 5 = 0.3 * 5 = 1.5
        assert fb.last_desired_total == pytest.approx(1.5)


class TestFeedbackSettled:

    def test_not_settled_freezes_feedback(self) -> None:
        fb = _make_fb(target_power_w=0.0)
        # First call: settled
        fb.calculate(
            phase_b_grid_power_w=[100.0],
            target_power_w=0.0,
            current_battery_output_w=0.0,
            other_corrections_w=0.0,
            settled=True,
        )
        prev_total = fb.last_desired_total

        # Second call: not settled → should NOT update desired_total
        fb.calculate(
            phase_b_grid_power_w=[500.0],
            target_power_w=0.0,
            current_battery_output_w=0.0,
            other_corrections_w=0.0,
            settled=False,
        )
        assert fb.last_desired_total == prev_total

    def test_empty_samples_does_not_update(self) -> None:
        fb = _make_fb()
        fb.calculate(
            phase_b_grid_power_w=[100.0],
            target_power_w=3.0,
            current_battery_output_w=50.0,
            other_corrections_w=0.0,
            settled=True,
        )
        prev_total = fb.last_desired_total

        fb.calculate(
            phase_b_grid_power_w=[],
            target_power_w=3.0,
            current_battery_output_w=50.0,
            other_corrections_w=0.0,
            settled=True,
        )
        assert fb.last_desired_total == prev_total


class TestFeedbackFeedforwardOffset:

    def test_ff_offset_subtracted_from_my_correction(self) -> None:
        fb = _make_fb(target_power_w=0.0, kp_draw=1.0, hysteresis_w=5.0)
        result = fb.calculate(
            phase_b_grid_power_w=[100.0],
            target_power_w=0.0,
            current_battery_output_w=0.0,
            other_corrections_w=50.0,
            settled=True,
        )
        # desired_total = 0 + correction
        # my_correction = desired_total - ff(50)
        assert result < fb.last_desired_total


class TestFeedbackDisabled:

    def test_disabled_returns_zero(self) -> None:
        fb = _make_fb(feedback_enabled=False)
        result = fb.calculate(
            phase_b_grid_power_w=[100.0],
            target_power_w=3.0,
            current_battery_output_w=50.0,
            other_corrections_w=30.0,
            settled=True,
        )
        assert result == 0.0

    def test_disabled_last_output_is_zero(self) -> None:
        fb = _make_fb(feedback_enabled=False)
        fb.calculate(
            phase_b_grid_power_w=[100.0],
            target_power_w=3.0,
            current_battery_output_w=50.0,
            other_corrections_w=0.0,
            settled=True,
        )
        assert fb.last_output == 0.0


class TestApplyEffectiveTotal:

    def test_anti_windup_aligns_state(self) -> None:
        fb = _make_fb(target_power_w=0.0, kp_draw=1.0, hysteresis_w=5.0)
        fb.calculate(
            phase_b_grid_power_w=[500.0],
            target_power_w=0.0,
            current_battery_output_w=0.0,
            other_corrections_w=0.0,
            settled=True,
        )
        # desired_total is high (e.g. 500)
        old_total = fb.last_desired_total
        assert old_total > 100.0

        # Battery clamped to 200 W
        fb.apply_effective_total(200.0, other_corrections_w=0.0)
        assert fb.last_desired_total == 200.0
        assert fb.last_output == 200.0

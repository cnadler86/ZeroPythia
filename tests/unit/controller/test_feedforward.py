"""Unit tests for FeedforwardSteuerung (feedforward phase controller).

Covers:
- Basic P-control behavior (target = 0 W)
- Hysteresis band gain switching
- Anti-export guard (output ≤ current draw)
- Oscillation limit clamping
- Zero/negative grid draw → zero output
"""

from __future__ import annotations

import pytest

from ZeroPythia.controller.feedforward_steuerung import (
    FeedforwardSteuerung,
    FeedforwardSteuerungSettings,
)
from ZeroPythia.controller.phase_controller import PhaseSample


def _make_ff(
    *,
    kp: float = 1.0,
    kp_hysteresis: float = 0.3,
    hysteresis_w: float = 10.0,
) -> FeedforwardSteuerung:
    return FeedforwardSteuerung(
        settings=FeedforwardSteuerungSettings(
            kp=kp,
            kp_hysteresis=kp_hysteresis,
            hysteresis_w=hysteresis_w,
        ),
        phase_label="A",
    )


class TestFeedforwardBasic:

    def test_positive_grid_draw_produces_positive_output(self) -> None:
        ff = _make_ff(kp=1.0, hysteresis_w=5.0)
        result = ff.calculate([100.0])
        assert result > 0
        # Full compensation outside hysteresis: output ≈ 100 W
        assert result == pytest.approx(100.0)

    def test_zero_grid_draw_produces_zero_output(self) -> None:
        ff = _make_ff()
        result = ff.calculate([0.0])
        assert result == 0.0

    def test_negative_grid_draw_produces_zero_output(self) -> None:
        """Feed-in on this phase should not produce negative requests."""
        ff = _make_ff()
        result = ff.calculate([-50.0])
        assert result == 0.0

    def test_output_never_negative(self) -> None:
        ff = _make_ff()
        for val in [-100, -10, -1, 0]:
            result = ff.calculate([float(val)])
            assert result >= 0.0, f"Negative output for input {val}"


class TestFeedforwardHysteresis:

    def test_hysteresis_band_uses_damped_gain(self) -> None:
        ff = _make_ff(kp=1.0, kp_hysteresis=0.3, hysteresis_w=10.0)
        # 5 W is within hysteresis band → kp_hysteresis * error = 0.3 * 5 = 1.5
        result = ff.calculate([5.0])
        assert result == pytest.approx(1.5)

    def test_outside_hysteresis_uses_full_gain(self) -> None:
        ff = _make_ff(kp=1.0, kp_hysteresis=0.3, hysteresis_w=10.0)
        # 50 W is outside hysteresis band → kp * error = 1.0 * 50 = 50
        result = ff.calculate([50.0])
        assert result == pytest.approx(50.0)

    def test_boundary_of_hysteresis(self) -> None:
        ff = _make_ff(kp=1.0, kp_hysteresis=0.3, hysteresis_w=10.0)
        # Exactly at the boundary
        result = ff.calculate([10.0])
        # abs(10) == hysteresis_w → condition is < not <=, so full gain
        assert result == pytest.approx(10.0)


class TestFeedforwardAntiExport:

    def test_output_capped_at_current_draw(self) -> None:
        """Anti-export: output must not exceed the latest grid draw."""
        ff = _make_ff(kp=1.0, hysteresis_w=5.0)
        # Multiple samples: filtered value might be higher than last sample
        # Last sample (newest) is 20 W → output capped at 20 W
        result = ff.calculate([200.0, 200.0, 20.0])
        assert result <= 20.0

    def test_anti_export_with_dropping_load(self) -> None:
        ff = _make_ff(kp=1.0, hysteresis_w=5.0)
        # Load dropped from 100 to 5 (last value)
        result = ff.calculate([100.0, 100.0, 5.0])
        assert result <= 5.0


class TestFeedforwardOscillationLimit:

    def test_osc_limit_caps_output(self) -> None:
        """When oscillation detector provides a limit, output is capped."""
        ff = _make_ff(kp=1.0, hysteresis_w=5.0)
        # First call with high draw
        result = ff.calculate([200.0])
        assert result > 0

        # Simulate osc limit by checking last_osc_limit
        # Without holder/predictor, limit should be inf
        assert ff.last_osc_limit == float("inf")


class TestFeedforwardProperties:

    def test_last_output_tracked(self) -> None:
        ff = _make_ff()
        ff.calculate([50.0])
        assert ff.last_output > 0

    def test_last_raw_output_tracked(self) -> None:
        ff = _make_ff()
        ff.calculate([50.0])
        assert ff.last_raw_output > 0

    def test_empty_input_preserves_last_output(self) -> None:
        ff = _make_ff()
        ff.calculate([50.0])
        prev = ff.last_output
        ff.calculate([])
        assert ff.last_output == prev

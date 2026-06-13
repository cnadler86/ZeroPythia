"""Unit tests for HysteresisPreprocessor.

Covers:
- Empty and single-value inputs
- Median-based filtering with outlier rejection
- Position-weighted averaging (linear and exponential)
- Hysteresis band behavior
"""

from __future__ import annotations

import pytest

from ZeroPythia.controller.pre_processor import HysteresisPreprocessor


class TestHysteresisPreprocessorBasic:
    """Basic input handling."""

    def test_empty_list_returns_none(self) -> None:
        pp = HysteresisPreprocessor(hysteresis=10.0)
        assert pp.process([]) is None

    def test_single_value_returned_as_is(self) -> None:
        pp = HysteresisPreprocessor(hysteresis=10.0)
        assert pp.process([42.0]) == 42.0

    def test_two_identical_values(self) -> None:
        pp = HysteresisPreprocessor(hysteresis=10.0)
        result = pp.process([100.0, 100.0])
        assert result == pytest.approx(100.0)

    def test_two_close_values_returns_weighted_mean(self) -> None:
        pp = HysteresisPreprocessor(hysteresis=10.0)
        # values: [100, 105], median=102.5, both within hysteresis=10
        # positions: [0, 1], weights: [1, 2], weighted = (100*1 + 105*2) / 3 = 310/3 ≈ 103.33
        result = pp.process([100.0, 105.0])
        assert result == pytest.approx(310.0 / 3.0)


class TestHysteresisPreprocessorOutlierRejection:
    """Outlier rejection via median + hysteresis band."""

    def test_outlier_is_filtered(self) -> None:
        pp = HysteresisPreprocessor(hysteresis=10.0)
        # values: [100, 102, 101, 500], median=101.5
        # inliers within ±10 of 101.5: [100, 102, 101] at positions [0, 1, 2]
        result = pp.process([100.0, 102.0, 101.0, 500.0])
        assert result is not None
        # Should be close to the inlier mean, NOT pulled toward 500
        assert 99.0 < result < 103.0

    def test_all_outliers_returns_median(self) -> None:
        """When fewer than 2 inliers exist, fallback to median."""
        pp = HysteresisPreprocessor(hysteresis=1.0)
        # values: [100, 200, 300] – median=200, hysteresis=1 → only [200] is inlier
        result = pp.process([100.0, 200.0, 300.0])
        assert result == pytest.approx(200.0)

    def test_negative_outlier_filtered(self) -> None:
        pp = HysteresisPreprocessor(hysteresis=10.0)
        # values: [-500, 100, 102, 101], median=100.5
        result = pp.process([-500.0, 100.0, 102.0, 101.0])
        assert result is not None
        assert 99.0 < result < 103.0


class TestHysteresisPreprocessorWeighting:
    """Position-based weighting (newer values = higher weight)."""

    def test_linear_weights_favor_later_values(self) -> None:
        pp = HysteresisPreprocessor(hysteresis=50.0, weight_type="linear")
        # values: [100, 200], both inliers (median=150, hysteresis=50)
        # positions: [0, 1], weights: [1, 2]
        # weighted = (100*1 + 200*2) / 3 = 500/3 ≈ 166.67
        result = pp.process([100.0, 200.0])
        assert result == pytest.approx(500.0 / 3.0)

    def test_exponential_weights_favor_later_values_more(self) -> None:
        pp = HysteresisPreprocessor(hysteresis=50.0, weight_type="exponential")
        # values: [100, 200], both inliers
        # positions: [0, 1], weights: [2^0, 2^1] = [1, 2]
        # Same as linear for 2 elements since weights happen to match
        result = pp.process([100.0, 200.0])
        assert result == pytest.approx(500.0 / 3.0)

    def test_linear_weights_with_gap_in_positions(self) -> None:
        """When an outlier creates a gap, weights should reflect actual positions."""
        pp = HysteresisPreprocessor(hysteresis=10.0, weight_type="linear")
        # values: [100, 500, 102], median=102, hysteresis=10
        # inliers: [100, 102] at positions [0, 2]
        # weights: [0+1, 2+1] = [1, 3]
        # weighted = (100*1 + 102*3) / 4 = 406/4 = 101.5
        result = pp.process([100.0, 500.0, 102.0])
        assert result == pytest.approx(406.0 / 4.0)

    def test_exponential_weights_with_gap_in_positions(self) -> None:
        pp = HysteresisPreprocessor(hysteresis=10.0, weight_type="exponential")
        # values: [100, 500, 102], median=102, hysteresis=10
        # inliers: [100, 102] at positions [0, 2]
        # weights: [2^0, 2^2] = [1, 4]
        # weighted = (100*1 + 102*4) / 5 = 508/5 = 101.6
        result = pp.process([100.0, 500.0, 102.0])
        assert result == pytest.approx(508.0 / 5.0)


class TestHysteresisPreprocessorEdgeCases:
    """Edge cases and boundary conditions."""

    def test_all_identical_values(self) -> None:
        pp = HysteresisPreprocessor(hysteresis=10.0)
        result = pp.process([50.0, 50.0, 50.0, 50.0])
        assert result == pytest.approx(50.0)

    def test_negative_values(self) -> None:
        pp = HysteresisPreprocessor(hysteresis=10.0)
        result = pp.process([-100.0, -98.0, -102.0])
        assert result is not None
        assert -103.0 < result < -97.0

    def test_zero_hysteresis_only_exact_median_is_inlier(self) -> None:
        pp = HysteresisPreprocessor(hysteresis=0.0)
        # values: [100, 200, 300], median=200, hysteresis=0 → only 200 is inlier
        result = pp.process([100.0, 200.0, 300.0])
        assert result == pytest.approx(200.0)

    def test_integer_inputs(self) -> None:
        pp = HysteresisPreprocessor(hysteresis=10)
        result = pp.process([100, 102, 101])
        assert result is not None
        assert isinstance(result, float)

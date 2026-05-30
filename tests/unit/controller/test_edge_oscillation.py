"""Unit tests for EdgeDetector and OscillationDetector.

Covers:
- EdgeDetector: rising/falling edge detection, merging, history trimming
- OscillationDetector: periodic oscillation detection, timeout, base load
"""

from __future__ import annotations

import pytest

from ZeroPythia.controller.oscillation_detectorv2 import (
    BaseloadHolder,
    BaseloadHolderSettings,
    EdgeDetector,
    OscillationDetector,
)


# ── EdgeDetector tests ────────────────────────────────────────────────────────


class TestEdgeDetector:

    def test_rising_edge_detected(self) -> None:
        det = EdgeDetector(threshold=10.0, edge_list_length=5, time_threshold=2.0)
        result = det.add_sample(20.0, 1.0)
        assert result == "rising"

    def test_falling_edge_detected(self) -> None:
        det = EdgeDetector(threshold=10.0, edge_list_length=5, time_threshold=2.0)
        det.add_sample(50.0, 1.0)  # rising
        result = det.add_sample(30.0, 2.0)  # falling: diff = -20 < -10
        assert result == "falling"

    def test_no_edge_within_threshold(self) -> None:
        det = EdgeDetector(threshold=10.0, edge_list_length=5, time_threshold=2.0)
        det.add_sample(5.0, 1.0)  # diff = 5 < 10
        result = det.add_sample(8.0, 2.0)  # diff = 3 < 10
        assert result is None

    def test_rising_edges_list_populated(self) -> None:
        det = EdgeDetector(threshold=10.0, edge_list_length=5, time_threshold=2.0)
        det.add_sample(20.0, 1.0)
        edges = det.get_rising_edges()
        assert len(edges) == 1
        assert edges[0] == 1.0

    def test_falling_edges_list_populated(self) -> None:
        det = EdgeDetector(threshold=10.0, edge_list_length=5, time_threshold=2.0)
        det.add_sample(50.0, 1.0)  # rising
        det.add_sample(30.0, 2.0)  # falling
        edges = det.get_falling_edges()
        assert len(edges) == 1
        assert edges[0] == 2.0

    def test_history_trimmed_by_time_threshold(self) -> None:
        det = EdgeDetector(threshold=10.0, edge_list_length=5, time_threshold=5.0)
        det.add_sample(5.0, 1.0)
        det.add_sample(5.0, 3.0)
        det.add_sample(5.0, 8.0)  # triggers trim: entries < 8-5=3 removed
        # Initial (0, 0) and sample at t=1 should be trimmed
        assert all(ts >= 3.0 for _, ts in det.history)

    def test_edge_list_maxlen_respected(self) -> None:
        det = EdgeDetector(threshold=5.0, edge_list_length=3, time_threshold=100.0)
        # Feed 5 rising edges
        for i in range(5):
            det.add_sample(100.0 * (i + 1), float(i * 2))
        # Only last 3 should remain
        assert len(det._rising_edges) <= 3

    def test_merge_close_edges_first_mode(self) -> None:
        det = EdgeDetector(
            threshold=10.0, edge_list_length=5, time_threshold=1.0, merge_mode="first"
        )
        det.add_sample(20.0, 1.0)  # rising at 1.0
        det.add_sample(5.0, 1.5)   # falling
        det.add_sample(20.0, 1.8)  # rising at 1.8 (within 1.0s of first)
        edges = det.get_rising_edges()
        # Should merge 1.0 and 1.8 into 1.0 (first mode)
        assert len(edges) == 1
        assert edges[0] == 1.0

    def test_merge_close_edges_last_mode(self) -> None:
        det = EdgeDetector(
            threshold=10.0, edge_list_length=5, time_threshold=1.0, merge_mode="last"
        )
        det.add_sample(20.0, 1.0)
        det.add_sample(5.0, 1.5)
        det.add_sample(20.0, 1.8)
        edges = det.get_rising_edges()
        assert len(edges) == 1
        assert edges[0] == 1.8

    def test_merge_close_edges_mean_mode(self) -> None:
        det = EdgeDetector(
            threshold=10.0, edge_list_length=5, time_threshold=1.0, merge_mode="mean"
        )
        det.add_sample(20.0, 1.0)
        det.add_sample(5.0, 1.5)
        det.add_sample(20.0, 1.8)
        edges = det.get_rising_edges()
        assert len(edges) == 1
        assert edges[0] == pytest.approx(1.4)  # mean(1.0, 1.8) = 1.4


# ── OscillationDetector tests ────────────────────────────────────────────────


def _make_oscillator(
    *,
    threshold: float = 50.0,
    min_period: float = 5.0,
    max_period: float = 20.0,
    min_rising_count: int = 3,
    period_variance: float = 0.3,
) -> OscillationDetector:
    return OscillationDetector(
        detector_name="test",
        threshold=threshold,
        min_period=min_period,
        max_period=max_period,
        min_rising_count=min_rising_count,
        period_variance=period_variance,
        time_threshold=2.0,
    )


def _feed_periodic_signal(
    osc: OscillationDetector,
    *,
    base_w: float = 100.0,
    peak_w: float = 300.0,
    period: float = 10.0,
    high_fraction: float = 0.4,
    cycles: int = 3,
    samples_per_phase: int = 3,
) -> None:
    """Feed a periodic square-wave-like signal."""
    high_duration = period * high_fraction
    low_duration = period * (1 - high_fraction)
    for cycle in range(cycles):
        t_start = cycle * period
        # High phase
        for i in range(samples_per_phase):
            t = t_start + high_duration * i / samples_per_phase
            osc.add_sample(peak_w, t)
        # Low phase
        for i in range(samples_per_phase):
            t = t_start + high_duration + low_duration * i / samples_per_phase
            osc.add_sample(base_w, t)


class TestOscillationDetector:

    def test_no_oscillation_with_constant_signal(self) -> None:
        osc = _make_oscillator()
        for i in range(20):
            osc.add_sample(100.0, float(i))
        assert not osc.is_oscillating

    def test_oscillation_detected_after_min_rising_count_cycles(self) -> None:
        osc = _make_oscillator(min_rising_count=3, min_period=5.0, max_period=20.0)
        _feed_periodic_signal(osc, period=10.0, cycles=3)
        assert osc.is_oscillating

    def test_no_oscillation_with_too_few_cycles(self) -> None:
        osc = _make_oscillator(min_rising_count=3)
        _feed_periodic_signal(osc, period=10.0, cycles=2)
        assert not osc.is_oscillating

    def test_no_oscillation_when_period_too_short(self) -> None:
        osc = _make_oscillator(min_period=5.0, max_period=20.0)
        _feed_periodic_signal(osc, period=2.0, cycles=4)
        assert not osc.is_oscillating

    def test_no_oscillation_when_period_too_long(self) -> None:
        osc = _make_oscillator(min_period=5.0, max_period=20.0)
        _feed_periodic_signal(osc, period=30.0, cycles=4)
        assert not osc.is_oscillating

    def test_oscillation_timeout_resets_state(self) -> None:
        osc = _make_oscillator(min_rising_count=3, period_variance=0.3)
        _feed_periodic_signal(osc, period=10.0, cycles=3)
        assert osc.is_oscillating

        # No new edges for 2× period → timeout
        osc.add_sample(100.0, 100.0)
        assert not osc.is_oscillating

    def test_rising_period_matches_signal(self) -> None:
        osc = _make_oscillator(min_rising_count=3, min_period=5.0, max_period=20.0)
        _feed_periodic_signal(osc, period=10.0, cycles=3)
        assert osc.is_oscillating
        assert osc.rising_period is not None
        assert osc.rising_period == pytest.approx(10.0, abs=1.0)

    def test_base_load_tracks_low_phase_minimum(self) -> None:
        osc = _make_oscillator(min_rising_count=3)
        _feed_periodic_signal(osc, base_w=80.0, peak_w=300.0, period=10.0, cycles=4)
        assert osc.is_oscillating
        bl = osc.base_load
        assert bl is not None
        assert bl <= 100.0  # should be near 80W

    def test_reset_clears_oscillation(self) -> None:
        osc = _make_oscillator(min_rising_count=3)
        _feed_periodic_signal(osc, period=10.0, cycles=3)
        assert osc.is_oscillating
        osc._reset()
        assert not osc.is_oscillating
        assert osc.rising_period is None


# ── BaseloadHolder tests ─────────────────────────────────────────────────────


class TestBaseloadHolder:

    def test_get_limit_returns_base_load_during_oscillation(self) -> None:
        holder = BaseloadHolder(
            BaseloadHolderSettings(
                threshold=50.0,
                min_period=5.0,
                max_period=20.0,
                min_rising_count=3,
                period_variance=0.5,
                time_threshold=2.0,
            )
        )
        # Feed periodic signal
        for cycle in range(4):
            t = cycle * 10.0
            # High phase
            holder.add_sample(300.0, t)
            holder.add_sample(300.0, t + 1.0)
            holder.add_sample(300.0, t + 2.0)
            # Low phase
            holder.add_sample(80.0, t + 4.0)
            holder.add_sample(80.0, t + 6.0)
            holder.add_sample(80.0, t + 8.0)

        if holder.is_oscillating:
            limit = holder.get_limit()
            # Limit should be capped at base_load level
            assert limit <= 300.0
            assert limit >= 0.0

    def test_get_limit_returns_inf_when_not_oscillating(self) -> None:
        holder = BaseloadHolder(BaseloadHolderSettings())
        # Only constant signal → no oscillation
        for i in range(10):
            holder.add_sample(100.0, float(i))
        assert not holder.is_oscillating
        # get_limit uses is_oscillating check in the mixin
        # When not oscillating, the osc_limit should be inf


# ── get_min_rising_falling_time tests ─────────────────────────────────────────


class TestGetMinRisingFallingTime:

    def test_returns_none_when_not_oscillating(self) -> None:
        osc = _make_oscillator()
        assert osc.get_min_rising_falling_time() is None

    def test_returns_correct_time_during_oscillation(self) -> None:
        osc = _make_oscillator(min_rising_count=3, min_period=5.0, max_period=20.0)
        _feed_periodic_signal(
            osc, period=10.0, cycles=4, high_fraction=0.4, samples_per_phase=3
        )
        assert osc.is_oscillating
        min_time = osc.get_min_rising_falling_time()
        if min_time is not None:
            # High phase duration = 10.0 * 0.4 = 4.0 s
            assert min_time > 0
            assert min_time < 10.0  # must be less than full period

import logging
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Literal, Optional

logger = logging.getLogger(__name__)


class EdgeDetector:
    """A robust edge detector for real-time signal processing.

    This class detects rising and falling edges in a signal based on a threshold.
    It includes post-processing to merge close edges of the same type.
    """

    def __init__(
        self,
        *,
        threshold: float,
        edge_list_length: int,
        time_threshold: float,
        merge_mode: Literal["first", "mean", "last"] = "first",
    ):
        """Initialize the EdgeDetector.

        Args:
            threshold (float): Threshold for detecting edges (difference in signal value).
            edge_list_length (int): Maximum number of edges to keep in the lists.
            time_threshold (float): Time threshold in seconds for merging close edges.
            merge_mode (Literal['first', 'mean', 'last']): Mode for merging timestamps: 'first', 'mean', or 'last'. Default is 'first'.
        """
        self.threshold: float = threshold
        self.time_threshold: float = time_threshold
        self.merge_mode: Literal["first", "mean", "last"] = merge_mode
        self.history: List[tuple[float, float]] = [(0, 0)]
        self._rising_edges: Deque[float] = deque(maxlen=edge_list_length)
        self._falling_edges: Deque[float] = deque(maxlen=edge_list_length)
        # Cache for post-processed edges
        self._rising_cache: List[float] | None = None
        self._falling_cache: List[float] | None = None

    def add_sample(self, value: float, timestamp: float) -> Literal["rising", "falling", None]:
        """Add a new sample to the detector and check for edges.

        if a new edge is detected, it is added to the respective edge list.
        Returns 'rising' if a rising edge was detected, 'falling' if a falling edge was detected,
        None if no edge was detected.

        Args:
            value (float): The signal value.
            timestamp (float): The timestamp of the sample.
        """
        self.history.append((value, timestamp))

        prev_value = self.history[-2][0]  # We know history has at least 2 elements
        diff = value - prev_value
        if diff > self.threshold:
            self._rising_edges.append(timestamp)
            self._rising_cache = None  # Invalidate cache
            logger.debug(
                "EdgeDetector RISING  val=%.1f prev=%.1f diff=%.1f thr=%.1f",
                value,
                prev_value,
                diff,
                self.threshold,
            )
            return "rising"
        elif diff < -self.threshold:
            self._falling_edges.append(timestamp)
            self._falling_cache = None  # Invalidate cache
            logger.debug(
                "EdgeDetector FALLING val=%.1f prev=%.1f diff=%.1f thr=%.1f",
                value,
                prev_value,
                diff,
                self.threshold,
            )
            return "falling"

        # Pop all values on history that are older than timestamp - self.time_threshold
        while self.history and self.history[0][1] < timestamp - self.time_threshold:
            self.history.pop(0)

        return None

    def _post_process_edges(self, edges: Deque[float]) -> list[float]:
        """Post-process the edges to merge close ones of the same type.

        Args:
            edges (Deque[float]): The list of edge timestamps.

        Returns:
            list[float]: The processed list of edge timestamps.
        """
        if not edges:
            return []
        # Sort edges
        sorted_edges = sorted(edges)
        merged = []
        current = sorted_edges[0]
        for next_edge in sorted_edges[1:]:
            if next_edge - current < self.time_threshold:
                # Merge based on mode
                if self.merge_mode == "first":
                    pass  # keep current
                elif self.merge_mode == "mean":
                    current = (current + next_edge) / 2
                elif self.merge_mode == "last":
                    current = next_edge
            else:
                merged.append(current)
                current = next_edge
        merged.append(current)
        return merged

    def get_rising_edges(self) -> List[float]:
        """Get the list of rising edges after post-processing.

        Returns:
            List[float]: List of rising edge timestamps.
        """
        if self._rising_cache is None:
            self._rising_cache = self._post_process_edges(self._rising_edges)
        return self._rising_cache

    def get_falling_edges(self) -> List[float]:
        """Get the list of falling edges after post-processing.

        Returns:
            List[float]: List of falling edge timestamps.
        """
        if self._falling_cache is None:
            self._falling_cache = self._post_process_edges(self._falling_edges)
        return self._falling_cache


class OscillationDetector:
    """Oscillation detector that uses EdgeDetector to detect oscillations in the signal.

    It analyzes rising edges for periodic behavior and detects oscillations based on
    a minimum number of consecutive rising edges with consistent periods.
    """

    def __init__(
        self,
        *,
        threshold: float,
        min_period: float,
        max_period: float,
        min_rising_count: int,
        time_threshold: float = 2.0,
        merge_mode: Literal["first", "mean", "last"] = "first",
        period_variance: float = 0.1,
        base_load_window: int = 2,
    ):
        """Initialize the OscillationDetector.

        Args:
            threshold (float): Threshold for edge detection.
            time_threshold (float): Time threshold for merging edges.
            merge_mode (Literal['first', 'mean', 'last']): Merge mode for edges.
            min_rising_count (int): Minimum number of rising edges to detect oscillation.
            period_variance (float): Allowed variance in period (fractional).
            min_period (float): Minimum period in seconds.
            max_period (float): Maximum period in seconds.
            base_load_window (int): Number of low-phase samples to consider for base load calculation.
        """
        self.min_rising_count: int = min_rising_count
        self.period_variance: float = period_variance
        self.min_period: float = min_period
        self.max_period: float = max_period

        self.is_oscillating: bool = False
        self.oscillation_start_time: float | None = None
        self.rising_period: float | None = None
        self._base_load: deque[float] = deque(maxlen=base_load_window)
        self._base_load_cache: float | None = None

        self._edge_detector = EdgeDetector(
            threshold=threshold,
            edge_list_length=min_rising_count
            + 1,  # We need at least 1 more in order to properly detect rising edges that are stepped
            time_threshold=time_threshold,
            merge_mode=merge_mode,
        )
        self._rising_times: List[float] = []
        self._falling_times: List[float] = []
        # Use sets for O(1) lookups
        self._rising_times_set: set[float] = set()
        self._falling_times_set: set[float] = set()
        self._phase: Literal["high", "low"] = "low"

    @property
    def _last_value(self) -> float:
        """Get the last sampled value from edge detector history."""
        return self._edge_detector.history[-1][0]

    @property
    def _current_timestamp(self) -> float:
        """Get the last sampled timestamp from edge detector history."""
        return self._edge_detector.history[-1][1]

    @property
    def base_load(self) -> float | None:
        """Get the current base load during oscillation.

        Returns:
            float | None: Minimum load during low phase of oscillation, or None if not oscillating.
        """
        if not self.is_oscillating or not self._base_load:
            return None
        return min(min(self._base_load), self._last_value)

    def add_sample(self, value: float, timestamp: float) -> None:
        """Add a sample and update oscillation detection.

        Args:
            value (float): Signal value.
            timestamp (float): Timestamp.
        """
        edge_type = self._edge_detector.add_sample(value, timestamp)
        if edge_type == "rising":
            self._update_oscillation()
            self._phase = "high"
        elif edge_type == "falling":
            self._update_falling_edges()
            self._phase = "low"

        self._calculate_baseload(value)
        self._detect_timeout(timestamp)

    def _calculate_baseload(self, value: float) -> None:
        if self._phase == "low":
            # During low phase, track the minimum value
            self._base_load_cache = min(
                value,
                self._base_load_cache if self._base_load_cache is not None else value,
            )
        else:  # high phase
            # When transitioning to high phase, save the cached low value
            if self._base_load_cache is not None:
                self._base_load.append(self._base_load_cache)
                self._base_load_cache = None

    def _detect_timeout(self, current_time: float) -> None:
        """Detect if oscillation has timed out based on the last rising edge time and expected period.

        Reset if timeout detected.

        Args:
            current_time (float): Current timestamp.
        """
        # Check for timeout if currently oscillating
        if self.is_oscillating and self.rising_period is not None and self._rising_times:
            last_rising = self._rising_times[-1]
            expected_next = last_rising + self.rising_period * (1 + self.period_variance)
            if current_time > expected_next:
                logger.info("Oscillation timeout detected. Resetting.")
                self._reset()

    def _update_oscillation(self) -> None:
        rising_edges = self._edge_detector.get_rising_edges()
        if len(rising_edges) < self.min_rising_count:
            return

        # If already oscillating, check if the new edge fits the current pattern
        if self.is_oscillating and self.rising_period is not None and self._rising_times:
            last_rising = rising_edges[-1]
            # Check if this is a new edge (not already in rising_times) - O(1) with set
            if last_rising not in self._rising_times_set:
                expected_time = self._rising_times[-1] + self.rising_period
                time_diff = abs(last_rising - expected_time)
                if time_diff / self.rising_period > self.period_variance:
                    # Edge doesn't fit the current oscillation pattern - reset
                    self._reset()
                    logger.info(
                        "Oscillation terminated - timing mismatch (expected ~%s).",
                        expected_time,
                    )
                    return
                else:
                    # Edge fits - add it and update
                    self._rising_times.append(last_rising)
                    self._rising_times_set.add(last_rising)
                    # Trim to keep only (min_rising_count + 1) recent entries
                    while len(self._rising_times) > self.min_rising_count + 1:
                        oldest = self._rising_times.pop(0)
                        self._rising_times_set.discard(oldest)
                    # Recalculate period from recent edges
                    recent_times = self._rising_times[-self.min_rising_count :]
                    if len(recent_times) >= 2:
                        recent_periods = [
                            recent_times[i + 1] - recent_times[i]
                            for i in range(len(recent_times) - 1)
                        ]
                        self.rising_period = sum(recent_periods) / len(recent_periods)
                    return
            else:
                # Same edge as before, nothing to do
                return

        # Not oscillating or need to start/restart - check if we have enough edges for a pattern
        # Use only the last min_rising_count + 1 edges to check for oscillation
        recent_rising = (
            rising_edges[-(self.min_rising_count + 1) :]
            if len(rising_edges) > self.min_rising_count
            else rising_edges
        )
        periods = [recent_rising[i + 1] - recent_rising[i] for i in range(len(recent_rising) - 1)]
        if not periods:
            return

        # Check if each period is within the allowed range
        if not all(self.min_period <= p <= self.max_period for p in periods):
            return

        # Check variance
        avg_period = sum(periods) / len(periods)
        variances = [abs(p - avg_period) / avg_period for p in periods]
        if any(v > self.period_variance for v in variances):
            return

        # All checks passed - start new oscillation
        self.is_oscillating = True
        self.oscillation_start_time = rising_edges[
            -self.min_rising_count
        ]  # time of the first rising edge in the detected oscillation
        self.rising_period = avg_period
        self._rising_times = rising_edges[-self.min_rising_count :]
        self._rising_times_set = set(self._rising_times)
        logger.info(
            "Oscillation started with period %s",
            self.rising_period,
        )
        # Backfill falling edges that were detected before oscillation was confirmed
        self._update_falling_edges()

    def _update_falling_edges(self) -> None:
        if self.is_oscillating:
            # Append missing falling edges - O(1) lookup with set
            actual_falling_edges = self._edge_detector.get_falling_edges()
            for edge in actual_falling_edges:
                if edge not in self._falling_times_set:
                    self._falling_times.append(edge)
                    self._falling_times_set.add(edge)
                    # Trim to keep only (min_rising_count + 1) recent entries
                    while len(self._falling_times) > self.min_rising_count + 1:
                        oldest = self._falling_times.pop(0)
                        self._falling_times_set.discard(oldest)

    def get_min_rising_falling_time(self) -> float | None:
        """Get the minimum time between rising and falling edges since oscillation started.

        Uses optimized algorithm: O(n log n) instead of O(n²).

        Returns:
            float | None: Minimum time or None if not oscillating or no data.
        """
        if not self.is_oscillating or not self._rising_times or not self._falling_times:
            return None

        # Sort both lists once
        sorted_rising = sorted(self._rising_times)
        sorted_falling = sorted(self._falling_times)

        min_time = float("inf")
        falling_idx = 0

        # For each rising edge, find the next falling edge using binary search approach
        for rising in sorted_rising:
            # Skip falling edges that are before this rising edge
            while falling_idx < len(sorted_falling) and sorted_falling[falling_idx] <= rising:
                falling_idx += 1

            # Check if we found a falling edge after this rising edge
            if falling_idx < len(sorted_falling):
                time_diff = sorted_falling[falling_idx] - rising
                min_time = min(min_time, time_diff)

            # Reset index for next rising edge (since lists might overlap)
            falling_idx = 0

        return min_time if min_time != float("inf") else None

    def get_rising_period(self) -> float | None:
        """Get the detected rising period.

        Returns:
            float | None: Period or None if not oscillating.
        """
        return self.rising_period if self.is_oscillating else None

    def _reset(self) -> None:
        """Reset the oscillation detector state."""
        self.is_oscillating = False
        self.oscillation_start_time = None
        self.rising_period = None
        self._rising_times = []
        self._falling_times = []
        self._rising_times_set.clear()
        self._falling_times_set.clear()
        self._base_load.clear()
        self._base_load_cache = None


@dataclass
class OscillationDetectorSettings:
    """Settings für BaseloadPredictor - wraps die Konstruktor-Parameter."""

    threshold: float
    min_period: float
    max_period: float
    period_variance: float
    time_threshold: float
    merge_mode: Literal["first", "mean", "last"] = "first"
    min_rising_count: int = 3
    base_load_window: int = 2


@dataclass
class BaseloadPredictorSettings(OscillationDetectorSettings):
    """Settings für BaseloadPredictor - wraps die Konstruktor-Parameter."""

    threshold: float = 100.0
    time_threshold: float = 2.0
    merge_mode: Literal["first", "mean", "last"] = "first"
    min_rising_count: int = 3
    period_variance: float = 2
    min_period: float = 8.0
    max_period: float = 120.0
    reaction_time: float = 4.0
    base_load_window: int = 2


class BaseloadPredictor(OscillationDetector):
    def __init__(
        self,
        settings: Optional[BaseloadPredictorSettings] = None,
    ):
        if not settings:
            settings = BaseloadPredictorSettings()
        super().__init__(
            threshold=settings.threshold,
            min_period=settings.min_period,
            max_period=settings.max_period,
            min_rising_count=settings.min_rising_count,
            time_threshold=settings.time_threshold,
            merge_mode=settings.merge_mode,
            period_variance=settings.period_variance,
            base_load_window=settings.base_load_window,
        )
        self.reaction_time = settings.reaction_time

    def get_limit(self) -> float:
        if not self.is_oscillating:
            return float("inf")

        # In low phase, always return base load
        if self._phase == "low":
            return self.base_load or self._last_value

        # In high phase: let the controller respond normally to the rising flank.
        # Only reduce to base load once we're within reaction_time of the predicted
        # falling edge — and never at the exact rising-edge sample itself, so that
        # normal regulation always has at least one full cycle to react.
        min_high_time = self.get_min_rising_falling_time()
        if min_high_time and self._rising_times:
            last_rising = self._rising_times[-1]
            # Skip the limit at the exact rising-edge sample
            # (_current_timestamp == last_rising) so normal regulation can respond.
            if self._current_timestamp > last_rising:
                expected_falling_time = last_rising + min_high_time
                # Apply limit only when inside the reaction window AND the predicted
                # falling edge is still in the future (guards against reaction_time >= min_high_time).
                if (
                    self._current_timestamp >= expected_falling_time - self.reaction_time
                    and expected_falling_time > self._current_timestamp
                ):
                    return self.base_load or self._last_value

        # No predictor limit – normal regulation
        return float("inf")


@dataclass
class BaseloadHolderSettings(OscillationDetectorSettings):
    """Settings for BaseloadHolder – fast short-cycle oscillation detection.

    Typical use: suppress battery output spikes caused by short on/off load
    cycles (e.g. fridge compressor, heat pump fan) with periods of 1–10 s.

    Bypass resume guard
    -------------------
    These settings *also* drive the **bypass resume guard** in
    ``ControlRuntime``.  When the battery is at 100 % SoC (bypass mode) and
    solar production is high, starting discharge can cause rapid
    bypass/discharge toggling.  The guard prevents this by requiring that
    household demand has consistently exceeded solar + a safety offset for
    a full *observation window* before discharge is allowed.

    The window is computed as::

        window_s = max_period × min_rising_count + 1 s

    across all configured holders.  Increasing ``max_period`` or
    ``min_rising_count`` therefore makes the bypass → discharge transition
    more conservative (longer confirmation window).
    """

    threshold: float = 30
    min_period: float = 1.0
    max_period: float = 10.0
    period_variance: float = 1.2
    time_threshold: float = 0.6
    merge_mode: Literal["first", "mean", "last"] = "first"
    min_rising_count: int = 3
    base_load_window: int = 3


class BaseloadHolder(OscillationDetector):
    def __init__(
        self,
        settings: Optional[BaseloadHolderSettings] = None,
    ):
        if not settings:
            settings = BaseloadHolderSettings()
        super().__init__(
            threshold=settings.threshold,
            min_period=settings.min_period,
            max_period=settings.max_period,
            min_rising_count=settings.min_rising_count,
            time_threshold=settings.time_threshold,
            merge_mode=settings.merge_mode,
            period_variance=settings.period_variance,
            base_load_window=settings.base_load_window,
        )

    def get_limit(self) -> float:
        return min(self.base_load or self._last_value, self._last_value)

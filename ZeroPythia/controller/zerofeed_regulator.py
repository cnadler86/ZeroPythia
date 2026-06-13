"""ZeroFeed regulator adapter.

Wraps the ZeroFeed control logic as a ``RegulatorBase`` implementation.
Driven by ``ControlRuntime`` – no internal asyncio tasks.

Architecture:
    Feedforward phases (all phases except control_phase):
        Individual P-steering per phase, target = 0 W.
        Oscillation detectors (Holder + Predictor) limit the battery request.
        Sum of all FF requests → variable target for the feedback phase.

    Feedback phase (control_phase, battery-connected):
        P-controller, variable target = -(ff_sum) + global_target_w.
        Oscillation detection on estimated real load:
            real_load ≈ phase_grid + battery_output - ff_sum

Configuration:
    Full Pydantic v2 model in ``ZeroPythia.config.zerofeed``.
    ``control_phase`` selects the battery phase; any of A, B, C is supported.
    Settings persisted to YAML with comment support (ruamel.yaml).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from pathlib import Path
from typing import Any, Optional

from ZeroPythia.config.zerofeed import (
    FeedbackPhaseConfig,
    FeedforwardPhaseConfig,
    ZeroFeedConfig,
    apply_config_update,
    current_settings,
    load_config,
    save_config,
)
from ZeroPythia.controller.feedforward_steuerung import (
    FeedforwardSteuerung,
    FeedforwardSteuerungSettings,
)
from ZeroPythia.controller.phase_controller import (
    InverterPhaseController,
    InverterPhaseControllerSettings,
    PhaseSample,
    _OscillationMixin,
)
from ZeroPythia.controller.regulator import BatteryInverterProtocol, RegulatorBase
from ZeroPythia.runtime.models import ControlStatus, GridSample, OscState

logger = logging.getLogger(__name__)


# ── Controller builder helpers ────────────────────────────────────────────────


def _osc_state_from(ctrl: _OscillationMixin) -> OscState:
    """Build an :class:`OscState` snapshot from any oscillation-aware controller.

    Both ``FeedforwardSteuerung`` and ``InverterPhaseController`` expose the same
    ``_OscillationMixin`` surface (``is_oscillating``, ``last_osc_limit``,
    ``holder``, ``predictor``), so the snapshot logic lives here once.
    """
    holder = ctrl.holder
    predictor = ctrl.predictor
    return OscState(
        oscillating=ctrl.is_oscillating,
        limit_w=ctrl.last_osc_limit if ctrl.is_oscillating else None,
        holder_active=holder is not None,
        predictor_active=predictor is not None,
        holder_oscillating=holder is not None and holder.is_oscillating,
        predictor_oscillating=predictor is not None and predictor.is_oscillating,
    )


def _build_ff(phase: str, ph_cfg: FeedforwardPhaseConfig) -> FeedforwardSteuerung:
    return FeedforwardSteuerung(
        settings=FeedforwardSteuerungSettings(
            kp=ph_cfg.kp,
            kp_hysteresis=ph_cfg.kp_hysteresis,
            hysteresis_w=ph_cfg.hysteresis_w,
        ),
        holder_settings=ph_cfg.osc.holder,
        predictor_settings=ph_cfg.osc.predictor,
        phase_label=phase,
    )


def _build_fb(
    phase: str,
    ph_cfg: FeedbackPhaseConfig,
    target_power_w: float,
) -> InverterPhaseController:
    return InverterPhaseController(
        settings=InverterPhaseControllerSettings(
            kp_draw=ph_cfg.kp_draw,
            kp_feed_in=ph_cfg.kp_feed_in,
            hysteresis_w=ph_cfg.hysteresis_w,
            kp_hysteresis=ph_cfg.kp_hysteresis,
            target_power_w=target_power_w,
            feedback_enabled=ph_cfg.feedback_enabled,
        ),
        holder_settings=ph_cfg.osc.holder,
        predictor_settings=ph_cfg.osc.predictor,
        phase_label=phase,
    )


# ── Internal sample ───────────────────────────────────────────────────────────


class _Sample:
    __slots__ = ("timestamp", "phases", "battery_output")

    def __init__(
        self,
        timestamp: float,
        phase_a: float,
        phase_b: float,
        phase_c: float,
        battery_output: float,
    ) -> None:
        self.timestamp = timestamp
        self.phases = {"A": phase_a, "B": phase_b, "C": phase_c}
        self.battery_output = battery_output


# ── Core ───────────────────────────────────────────────────────────────────


class _Core:
    """Holds all phase controllers and implements the control + watchdog logic.

    The feedback (battery) phase is determined by ``cfg.control_phase``.
    All other phases use feedforward controllers.
    """

    def __init__(self, cfg: ZeroFeedConfig) -> None:
        self._cfg = cfg
        self._ff: dict[str, FeedforwardSteuerung] = {}
        self._fb: Optional[InverterPhaseController] = None
        self._rebuild_controllers()

    def _rebuild_controllers(self) -> None:
        cfg = self._cfg
        self._ff = {}
        for ph, ph_cfg in cfg.phases.items():
            if isinstance(ph_cfg, FeedforwardPhaseConfig):
                self._ff[ph] = _build_ff(ph, ph_cfg)
        fb_cfg = cfg.phases.get(cfg.control_phase)
        if not isinstance(fb_cfg, FeedbackPhaseConfig):
            raise ValueError(f"control_phase={cfg.control_phase!r} has no FeedbackPhaseConfig")
        self._fb = _build_fb(cfg.control_phase, fb_cfg, cfg.target_power_w)

        ff_summary = ", ".join(
            f"{ph}(holder={'on' if ctrl.holder else 'off'}, predictor={'on' if ctrl.predictor else 'off'})"
            for ph, ctrl in sorted(self._ff.items())
        )
        fb_summary = (
            f"{cfg.control_phase}(holder={'on' if self._fb.holder else 'off'}, "
            f"predictor={'on' if self._fb.predictor else 'off'})"
            if self._fb is not None
            else "none"
        )
        logger.info(
            "ZeroFeed controllers built: control_phase=%s ff=[%s] fb=%s",
            cfg.control_phase,
            ff_summary or "none",
            fb_summary,
        )

    # ── Control calculation ───────────────────────────────────────────────────

    def calculate(
        self,
        phase_samples: dict[str, list[PhaseSample]],
        batt_hist: list[float],
        current_battery_output_w: float,
        battery_settled: bool,
    ) -> tuple[dict[str, float], float, float]:
        """Run one ZeroFeed control cycle.

        Returns:
            (ff_outputs, fb_correction, ff_sum)
            where ``ff_outputs`` maps phase name → FF demand,
            ``fb_correction`` is the feedback controller output, and
            ``ff_sum`` is the sum of all feedforward demands.
        """
        if self._fb is None:
            raise RuntimeError("Feedback controller not initialized")

        # 1) Feedforward for all non-battery phases
        ff_outputs: dict[str, float] = {}
        for ph, ff_ctrl in self._ff.items():
            samples = phase_samples.get(ph, [])
            vals = [s.value for s in samples]
            ff_outputs[ph] = ff_ctrl.calculate(vals, osc_samples=samples)

        ff_sum = sum(ff_outputs.values())

        # 2) Estimated real load on the feedback phase (for oscillation detection only)
        ctrl_ph = self._cfg.control_phase
        ctrl_samples = phase_samples.get(ctrl_ph, [])
        ctrl_vals = [s.value for s in ctrl_samples]

        real_fb_samples: Optional[list[PhaseSample]] = None
        if ctrl_samples and batt_hist:
            n = min(len(ctrl_samples), len(batt_hist))
            # Estimated real load on the feedback phase:
            #   real_load = phase_grid + battery_output
            # The ff_sum (A+C compensation) must NOT be subtracted here because
            # phases A/B/C are electrically independent – changes on A/C do not
            # affect the Phase-B grid reading.  Subtracting ff_sum would make
            # all samples negative whenever the battery over-compensates A+C,
            # starving the oscillation detector of positive samples.
            real_fb_samples = [
                PhaseSample(
                    timestamp=ctrl_samples[i].timestamp,
                    value=ctrl_samples[i].value + batt_hist[i],
                )
                for i in range(n)
            ]
            # DIAG: summarize estimated real-load samples for oscillation analysis.
            _vals = [s.value for s in real_fb_samples]
            _pos = sum(1 for v in _vals if v > 0)
            logger.debug(
                "OSC-DIAG[%s]: ff_sum=%.0fW batt_last=%.0fW ctrl_last=%.0fW "
                "real_fb_last=%.1fW min=%.1fW max=%.1fW pos=%d/%d",
                ctrl_ph,
                ff_sum,
                batt_hist[-1] if batt_hist else float("nan"),
                ctrl_samples[-1].value if ctrl_samples else float("nan"),
                _vals[-1],
                min(_vals),
                max(_vals),
                _pos,
                len(_vals),
            )

        # 3) Feedback phase controller
        fb_correction = self._fb.calculate(
            phase_b_grid_power_w=ctrl_vals,
            target_power_w=self._cfg.target_power_w,
            current_battery_output_w=current_battery_output_w,
            other_corrections_w=ff_sum,
            settled=battery_settled,
            osc_samples=real_fb_samples,
        )

        return ff_outputs, fb_correction, ff_sum

    def apply_effective_total(self, effective_total_w: float, ff_sum: float) -> None:
        """Synchronize feedback internals with the actually applied battery setpoint."""
        if self._fb is None:
            return
        self._fb.apply_effective_total(
            effective_total_w=effective_total_w, other_corrections_w=ff_sum
        )

    # ── Full reset ────────────────────────────────────────────────────────────

    def reset(self) -> None:
        self._rebuild_controllers()

    # ── Oscillation state ─────────────────────────────────────────────────────

    def osc_state(self, phase: str) -> OscState:
        """Return full oscillation state for one phase (feedback or feedforward)."""
        if phase == self._cfg.control_phase:
            if self._fb is None:
                raise RuntimeError("Feedback controller not initialized")
            return _osc_state_from(self._fb)
        ff = self._ff.get(phase)
        return _osc_state_from(ff) if ff is not None else OscState()


# ── Dynamic settings schema helper ───────────────────────────────────────────

_MERGE_MODES = ["first", "mean", "last"]


def _num(title: str, default: float, minimum: float, maximum: float, step: float) -> dict[str, Any]:
    """JSON-schema entry for a float field (``group`` is added by the caller)."""
    return {
        "type": "number",
        "title": title,
        "default": default,
        "minimum": minimum,
        "maximum": maximum,
        "step": step,
    }


def _int(title: str, default: int, minimum: int, maximum: int) -> dict[str, Any]:
    """JSON-schema entry for an integer field."""
    return {
        "type": "integer",
        "title": title,
        "default": default,
        "minimum": minimum,
        "maximum": maximum,
    }


def _bool(title: str, default: bool) -> dict[str, Any]:
    """JSON-schema entry for a boolean field."""
    return {"type": "boolean", "title": title, "default": default}


def _enum(title: str, default: str, choices: list[str]) -> dict[str, Any]:
    """JSON-schema entry for a string enum field."""
    return {"type": "string", "title": title, "default": default, "enum": choices}


def _phase_schema(ph_name: str, ph_cfg) -> dict[str, Any]:
    """Build nested-path settings schema entries for one phase.

    Keys use dot-notation mirroring the ``ZeroFeedConfig`` model structure
    (e.g. ``'phases.B.kp_draw'``).  Virtual boolean fields
    ``phases.X.osc.holder_enabled`` / ``phases.X.osc.predictor_enabled``
    map to the presence / absence of the ``holder`` / ``predictor`` sub-model.
    """
    base = f"phases.{ph_name}."
    osc = base + "osc."
    is_fb = isinstance(ph_cfg, FeedbackPhaseConfig)
    group = f"Phase {ph_name} ({'Regulation' if is_fb else 'Steering'})"

    # Ordered (key, spec) list; ``group`` is attached uniformly at the end.
    # Declaring every field as a one-line table makes ranges/defaults easy to scan
    # and keeps holder vs. predictor differences visible side by side.
    fields: list[tuple[str, dict[str, Any]]] = []

    if is_fb:
        fields += [
            (base + "kp_draw", _num("Kp draw", 0.9, 0.0, 5.0, 0.05)),
            (base + "kp_feed_in", _num("Kp feed-in", 1.05, 0.0, 5.0, 0.05)),
            (base + "feedback_enabled", _bool("Feedback enabled", True)),
        ]
    else:
        fields.append((base + "kp", _num("Kp", 1.0, 0.0, 5.0, 0.05)))

    fields += [
        (base + "kp_hysteresis", _num("Kp hysteresis", 0.4, 0.0, 2.0, 0.05)),
        (base + "hysteresis_w", _num("Hysteresis band [W]", 5.0, 0.0, 100.0, 0.5)),
        # ── Holder – fast short-cycle oscillations ──────────────────────────────
        (osc + "holder_enabled", _bool("Holder active (fast oscillations)", False)),
        (osc + "holder.threshold", _num("Holder min. amplitude [W]", 30.0, 5.0, 500.0, 5.0)),
        (osc + "holder.min_period", _num("Holder min. period [s]", 1.0, 0.1, 60.0, 0.1)),
        (osc + "holder.max_period", _num("Holder max. period [s]", 10.0, 0.1, 600.0, 0.1)),
        (osc + "holder.period_variance", _num("Holder period variance", 1.2, 0.01, 10.0, 0.05)),
        (osc + "holder.time_threshold", _num("Holder time threshold [s]", 0.6, 0.01, 30.0, 0.05)),
        (osc + "holder.min_rising_count", _int("Holder min. rising edges", 3, 2, 20)),
        (osc + "holder.merge_mode", _enum("Holder merge mode", "first", _MERGE_MODES)),
        (osc + "holder.base_load_window", _int("Holder baseload window", 3, 1, 20)),
        # ── Predictor – slow periodic loads ─────────────────────────────────────
        (osc + "predictor_enabled", _bool("Predictor active (periodic loads)", True)),
        (
            osc + "predictor.threshold",
            _num("Predictor min. amplitude [W]", 100.0, 10.0, 1000.0, 10.0),
        ),
        (osc + "predictor.min_period", _num("Predictor min. period [s]", 8.0, 0.1, 600.0, 0.1)),
        (osc + "predictor.max_period", _num("Predictor max. period [s]", 120.0, 0.1, 3600.0, 0.1)),
        (
            osc + "predictor.period_variance",
            _num("Predictor period variance", 2.0, 0.01, 10.0, 0.05),
        ),
        (
            osc + "predictor.time_threshold",
            _num("Predictor time threshold [s]", 2.0, 0.01, 120.0, 0.05),
        ),
        (osc + "predictor.min_rising_count", _int("Predictor min. rising edges", 3, 2, 20)),
        (osc + "predictor.merge_mode", _enum("Predictor merge mode", "first", _MERGE_MODES)),
        (osc + "predictor.base_load_window", _int("Predictor baseload window", 2, 1, 20)),
        (
            osc + "predictor.reaction_time",
            _num("Predictor reaction time [s]", 4.0, 0.0, 300.0, 0.1),
        ),
    ]

    return {key: {**spec, "group": group} for key, spec in fields}


# ── Regulator ─────────────────────────────────────────────────────────────────


class ZeroFeedRegulator(RegulatorBase):
    """ZeroFeed – configurable-phase FF+FB zero-feed-in controller.

    ``control_phase`` selects which phase carries the battery inverter.
    All other phases are controlled via feedforward steering.
    Settings are persisted to YAML (ruamel.yaml, comments preserved).
    """

    _NAME = "zerofeed"
    _DESC = (
        "Configurable regulation phase (battery phase). "
        "All other phases: feedforward steering. "
        "Per-phase individual settings, YAML-persistent."
    )

    def __init__(
        self,
        settings: Optional[ZeroFeedConfig] = None,
        yaml_path: Optional[Path] = None,
    ) -> None:
        self._yaml_path = yaml_path

        # Load from YAML if available, else use provided/default settings
        loaded = load_config(yaml_path) if yaml_path is not None else None
        cfg = loaded if loaded is not None else (settings or ZeroFeedConfig())

        self._cfg = cfg
        self._core = _Core(cfg)
        self._queue: asyncio.Queue[_Sample] = asyncio.Queue(maxsize=cfg.queue_size())
        self._current_setpoint: int = 0
        self._last_requested_setpoint: int = 0
        self._last_status: ControlStatus = ControlStatus(regulator_name=self._NAME, setpoint_w=0)

        # ── PT1 battery output model ───────────────────────────────────────────
        # Instead of reading the (delayed) battery API, we model the response as:
        #   dead time  (battery_dead_time_s)  → then PT1 ramp  (battery_pt1_tau_s)
        # Timestamps are wall-clock (time.time()) to match GridSample.timestamp.
        self._sp_sent_at: float = 0.0  # wall time when last setpoint was sent
        self._sp_target_w: float = 0.0  # setpoint sent
        self._sp_prev_output_w: float = 0.0  # estimated output just before the change

        if loaded is None and yaml_path is not None and not yaml_path.exists():
            # Write initial YAML with comments when no file exists yet
            save_config(yaml_path, cfg)

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._NAME

    @property
    def description(self) -> str:
        return self._DESC

    # ── PT1 battery model ────────────────────────────────────────────────────

    def _estimate_batt_at(self, ts: float) -> float:
        """Estimate battery AC output [W] at wall-clock timestamp *ts*.

        Models the battery as dead-time + PT1:
          – During dead time:       output stays at previous level.
          – After dead time:        PT1 ramp toward the new setpoint.
          – If no setpoint was ever sent: returns 0.
        """
        if self._sp_sent_at == 0.0:
            return 0.0
        dead = self._cfg.battery_dead_time_s
        tau = self._cfg.battery_pt1_tau_s
        elapsed = ts - (self._sp_sent_at + dead)
        if elapsed <= 0:
            return self._sp_prev_output_w
        factor = 1.0 - math.exp(-elapsed / tau) if tau > 0 else 1.0
        return self._sp_prev_output_w + (self._sp_target_w - self._sp_prev_output_w) * factor

    # ── Sampling ──────────────────────────────────────────────────────────────

    async def add_sample(self, sample: GridSample) -> None:
        s = _Sample(
            timestamp=sample.timestamp,
            phase_a=sample.phase_a_w,
            phase_b=sample.phase_b_w,
            phase_c=sample.phase_c_w,
            battery_output=sample.battery_output_w,
        )
        try:
            self._queue.put_nowait(s)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(s)
            except asyncio.QueueEmpty:
                pass

    # ── Control ───────────────────────────────────────────────────────────────

    async def compute_setpoint(
        self,
        battery: BatteryInverterProtocol,
        max_output_w: int,
        min_output_w: int,
    ) -> Optional[int]:
        if self._queue.empty():
            return None

        # Drain queue – build PT1-estimated battery history per sample timestamp
        phase_samples: dict[str, list[PhaseSample]] = {"A": [], "B": [], "C": []}
        batt_hist: list[float] = []

        while not self._queue.empty():
            try:
                s = self._queue.get_nowait()
                ts = s.timestamp
                for ph in ("A", "B", "C"):
                    phase_samples[ph].append(PhaseSample(timestamp=ts, value=s.phases[ph]))
                # Use PT1 model instead of API-reported value (API has ~1.5 s delay)
                batt_hist.append(self._estimate_batt_at(ts))
            except asyncio.QueueEmpty:
                break

        if not phase_samples["A"]:
            return None

        # Settlement check – only API call we still need
        settled = await battery.is_settled(use_cache=False)
        battery_settled = settled is not False
        # Estimate current battery output from PT1 model at current wall time
        batt_now = self._estimate_batt_at(time.time())

        # Core ZeroFeed calculation
        ff_outputs, fb_correction, ff_sum = self._core.calculate(
            phase_samples=phase_samples,
            batt_hist=batt_hist,
            current_battery_output_w=batt_now,
            battery_settled=battery_settled,
        )

        raw_target = ff_sum + fb_correction

        # Lower bound stays in the regulator. Upper clamping is delegated to
        # the battery implementation so hardware-specific constraints are applied
        # in one place (e.g. inverse_max_power, SoC-dependent limits).
        requested_sp = int(round(max(float(min_output_w), raw_target)))

        changed = False
        if requested_sp != self._last_requested_setpoint:
            self._last_requested_setpoint = requested_sp
            applied_sp = await battery.set_ac_output_limit(requested_sp)
            if applied_sp >= 0:
                # Record timing for PT1 model BEFORE updating current_setpoint
                now = time.time()
                self._sp_prev_output_w = self._estimate_batt_at(now)
                self._sp_sent_at = now
                self._sp_target_w = float(applied_sp)

                changed = applied_sp != self._current_setpoint
                self._current_setpoint = applied_sp
                self._core.apply_effective_total(effective_total_w=applied_sp, ff_sum=ff_sum)
                logger.debug(
                    "ZeroFeed setpoint request=%d W applied=%d W  (ff=%s  fb=%.0fW  ff_sum=%.0fW  ctrl=%s)",
                    requested_sp,
                    applied_sp,
                    {ph: f"{v:.0f}" for ph, v in ff_outputs.items()},
                    fb_correction,
                    ff_sum,
                    self._cfg.control_phase,
                )
            else:
                self._last_requested_setpoint = self._current_setpoint
                logger.warning(
                    "ZeroFeed setpoint request=%d W failed at battery layer", requested_sp
                )

        self._last_status = ControlStatus(
            regulator_name=self._NAME,
            setpoint_w=self._current_setpoint,
            setpoint_changed=changed,
            raw_target_w=raw_target,
            target_power_w=self._cfg.target_power_w,
            ff_output_w=ff_sum,
            feedback_output_w=fb_correction,
            ff_per_phase=ff_outputs,
            osc_limit_w=self._core.osc_state(self._cfg.control_phase).limit_w,
            osc_a=self._core.osc_state("A"),
            osc_b=self._core.osc_state("B"),
            osc_c=self._core.osc_state("C"),
        )

        return self._current_setpoint if changed else None

    # ── Bypass guard window ───────────────────────────────────────────────────

    def bypass_resume_window_s(self) -> float:
        """Compute the bypass → discharge safety window from oscillation-holder configs.

        Formula:
            window = max_period_across_holders × max_min_rising_count_across_holders + 1 s

        Rationale
        ---------
        ``max_period`` is the longest oscillation period a holder can detect.
        ``min_rising_count`` is the number of rising edges required to confirm
        an oscillation.  The product gives the minimum time span the holder needs
        to observe before it can flag an oscillation; adding 1 s provides a small
        buffer.  If we wait this long *before* starting discharge we can be
        confident that the discharge will not immediately re-trigger the
        bypass/discharge toggling pattern.

        Falls back to 25.0 s when no holders are configured.
        """
        holders = [
            ph_cfg.osc.holder
            for ph_cfg in self._cfg.phases.values()
            if ph_cfg.osc.holder is not None
        ]
        if not holders:
            return 25.0
        max_period = max(h.max_period for h in holders)
        max_rising_count = max(h.min_rising_count for h in holders)
        return max_period * max_rising_count + 1.0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def reset(self) -> None:
        self._core.reset()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._current_setpoint = 0
        self._last_requested_setpoint = 0
        self._last_status = ControlStatus(regulator_name=self._NAME, setpoint_w=0)
        self._sp_sent_at = 0.0
        self._sp_target_w = 0.0
        self._sp_prev_output_w = 0.0

    # ── Status ────────────────────────────────────────────────────────────────

    def get_control_status(self) -> ControlStatus:
        return self._last_status

    # ── Settings ──────────────────────────────────────────────────────────────

    def settings_schema(self) -> dict[str, Any]:
        cfg = self._cfg
        schema: dict[str, Any] = {
            "control_phase": {
                "type": "string",
                "title": "Regulation phase (battery phase)",
                "default": "B",
                "enum": ["A", "B", "C"],
                "group": "General",
                "description": "Changing this resets the phase controllers.",
            },
            "target_power_w": {
                "type": "number",
                "title": "Target draw [W]",
                "default": 3.0,
                "minimum": -50.0,
                "maximum": 100.0,
                "step": 1.0,
                "group": "General",
            },
            "control_interval_s": {
                "type": "number",
                "title": "Control interval [s]",
                "default": 3.0,
                "minimum": 1.0,
                "maximum": 30.0,
                "step": 0.5,
                "group": "General",
            },
            "battery_dead_time_s": {
                "type": "number",
                "title": "Battery dead time [s]",
                "default": 1.1,
                "minimum": 0.0,
                "maximum": 5.0,
                "step": 0.1,
                "group": "General",
            },
            "battery_pt1_tau_s": {
                "type": "number",
                "title": "Battery PT1 time constant [s]",
                "default": 0.5,
                "minimum": 0.0,
                "maximum": 5.0,
                "step": 0.1,
                "group": "General",
            },
        }
        for ph in ("A", "B", "C"):
            ph_cfg = cfg.phases.get(ph)
            if ph_cfg is not None:
                schema.update(_phase_schema(ph, ph_cfg))
        return schema

    def get_current_settings(self) -> dict[str, Any]:
        return current_settings(self._cfg)

    def apply_settings(self, data: dict[str, Any]) -> None:
        old_cp = self._cfg.control_phase
        new_cfg = apply_config_update(data, self._cfg)
        self._cfg = new_cfg
        self._core = _Core(new_cfg)
        self._queue = asyncio.Queue(maxsize=new_cfg.queue_size())
        # PT1 state is kept (setpoint timing remains valid after param change)

        if self._yaml_path is not None:
            save_config(
                self._yaml_path,
                new_cfg,
                old_control_phase=old_cp if old_cp != new_cfg.control_phase else None,
            )

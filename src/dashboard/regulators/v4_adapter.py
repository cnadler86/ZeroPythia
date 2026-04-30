"""ZeroFeed V4 regulator adapter.

Wraps the ZeroFeed V4 control logic as a ``RegulatorBase`` implementation.
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

    Watchdog (cycle-based):
        Counts consecutive control cycles with total grid feed-in outside the
        hysteresis band.  After ``watchdog_cycles`` violations, resets only the
        phase controllers that individually show feed-in outside their own
        hysteresis.

Configuration:
    Full Pydantic v2 model in ``src.config.zerofeed_v4``.
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

from src.config.zerofeed_v4 import (
    FeedbackPhaseConfig,
    FeedforwardPhaseConfig,
    ZeroFeedV4Config,
    config_to_flat,
    flat_to_config,
    load_config,
    save_config,
)
from src.controller.feedforward_steuerung import FeedforwardSteuerung, FeedforwardSteuerungSettings
from src.controller.oscillation_detectorv2 import BaseloadHolderSettings, BaseloadPredictorSettings
from src.controller.phase_controller import (
    InverterPhaseController,
    InverterPhaseControllerSettings,
    PhaseSample,
)
from src.dashboard.models import ControlStatus, GridSample, OscState
from src.dashboard.regulator import BatteryInverterProtocol, RegulatorBase

logger = logging.getLogger(__name__)


# ── Controller builder helpers ────────────────────────────────────────────────


def _make_holder(osc) -> Optional[BaseloadHolderSettings]:
    """Return the holder settings, or None if disabled."""
    return osc.holder


def _make_predictor(osc) -> Optional[BaseloadPredictorSettings]:
    """Return the predictor settings, or None if disabled."""
    return osc.predictor


def _build_ff(ph_cfg: FeedforwardPhaseConfig) -> FeedforwardSteuerung:
    return FeedforwardSteuerung(
        settings=FeedforwardSteuerungSettings(
            kp=ph_cfg.kp,
            kp_hysteresis=ph_cfg.kp_hysteresis,
            hysteresis_w=ph_cfg.hysteresis_w,
        ),
        holder_settings=_make_holder(ph_cfg.osc),
        predictor_settings=_make_predictor(ph_cfg.osc),
    )


def _build_fb(ph_cfg: FeedbackPhaseConfig, target_power_w: float) -> InverterPhaseController:
    return InverterPhaseController(
        settings=InverterPhaseControllerSettings(
            kp_draw=ph_cfg.kp_draw,
            kp_feed_in=ph_cfg.kp_feed_in,
            hysteresis_w=ph_cfg.hysteresis_w,
            kp_hysteresis=ph_cfg.kp_hysteresis,
            target_power_w=target_power_w,
            feedback_enabled=ph_cfg.feedback_enabled,
        ),
        holder_settings=_make_holder(ph_cfg.osc),
        predictor_settings=_make_predictor(ph_cfg.osc),
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


# ── V4 Core ───────────────────────────────────────────────────────────────────


class _V4Core:
    """Holds all phase controllers and implements the V4 control + watchdog logic.

    The feedback (battery) phase is determined by ``cfg.control_phase``.
    All other phases use feedforward controllers.
    """

    def __init__(self, cfg: ZeroFeedV4Config) -> None:
        self._cfg = cfg
        self._ff: dict[str, FeedforwardSteuerung] = {}
        self._fb: Optional[InverterPhaseController] = None
        self._violation_count: int = 0
        self._rebuild_controllers()

    def _rebuild_controllers(self) -> None:
        cfg = self._cfg
        self._ff = {}
        for ph, ph_cfg in cfg.phases.items():
            if isinstance(ph_cfg, FeedforwardPhaseConfig):
                self._ff[ph] = _build_ff(ph_cfg)
        fb_cfg = cfg.phases.get(cfg.control_phase)
        if not isinstance(fb_cfg, FeedbackPhaseConfig):
            raise ValueError(f"control_phase={cfg.control_phase!r} has no FeedbackPhaseConfig")
        self._fb = _build_fb(fb_cfg, cfg.target_power_w)

    # ── Control calculation ───────────────────────────────────────────────────

    def calculate(
        self,
        phase_samples: dict[str, list[PhaseSample]],
        batt_hist: list[float],
        current_battery_output_w: float,
        battery_settled: bool,
    ) -> tuple[dict[str, float], float, float]:
        """Run one V4 control cycle.

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
            # DIAG: log real_fb_samples to debug oscillation detection starvation
            _vals = [round(s.value, 1) for s in real_fb_samples]
            _pos = sum(1 for v in _vals if v > 0)
            logger.debug(
                "OSC-DIAG [%s]: ff_sum=%.0fW  batt_hist[-1]=%.0fW  "
                "ctrl_grid[-1]=%.0fW  real_fb_samples=%s  pos_count=%d/%d",
                ctrl_ph,
                ff_sum,
                batt_hist[-1] if batt_hist else float("nan"),
                ctrl_samples[-1].value if ctrl_samples else float("nan"),
                _vals,
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

    # ── Watchdog ──────────────────────────────────────────────────────────────

    def check_watchdog(
        self,
        last_phase_values: dict[str, float],
        ff_sum: float,
    ) -> list[str]:
        """Cycle-based watchdog.  Returns list of phase names that were reset."""
        cfg = self._cfg
        total = sum(last_phase_values.values())
        max_hyst = max(ph_cfg.hysteresis_w for ph_cfg in cfg.phases.values())
        outside_total = total < (cfg.target_power_w - max_hyst)

        if not outside_total:
            self._violation_count = 0
            return []

        self._violation_count += 1
        if self._violation_count <= cfg.watchdog_cycles:
            return []

        reset_phases: list[str] = []

        # Check each feedforward phase individually
        for ph, ph_cfg in cfg.phases.items():
            if isinstance(ph_cfg, FeedforwardPhaseConfig):
                if last_phase_values.get(ph, 0.0) < -ph_cfg.hysteresis_w:
                    self._ff[ph] = _build_ff(ph_cfg)
                    reset_phases.append(ph)

        # Check feedback phase
        fb_cfg = cfg.phases.get(cfg.control_phase)
        if isinstance(fb_cfg, FeedbackPhaseConfig):
            ctrl_val = last_phase_values.get(cfg.control_phase, 0.0)
            fb_target = cfg.target_power_w - ff_sum
            if ctrl_val < fb_target - fb_cfg.hysteresis_w:
                self._fb = _build_fb(fb_cfg, cfg.target_power_w)
                reset_phases.append(cfg.control_phase)

        if reset_phases:
            self._violation_count = 0
            logger.warning(
                "V4 watchdog: reset phase(s) %s after %d cycles of feed-in "
                "(total=%.1fW  threshold=%.1fW)",
                "+".join(sorted(reset_phases)),
                cfg.watchdog_cycles,
                total,
                cfg.target_power_w - max_hyst,
            )

        return reset_phases

    # ── Full reset ────────────────────────────────────────────────────────────

    def reset(self) -> None:
        self._rebuild_controllers()
        self._violation_count = 0

    # ── Oscillation state ─────────────────────────────────────────────────────

    def osc_state(self, phase: str) -> OscState:
        """Return full oscillation state for one phase."""
        if phase == self._cfg.control_phase:
            if self._fb is None:
                raise RuntimeError("Feedback controller not initialized")
            ctrl = self._fb
            return OscState(
                oscillating=ctrl.is_oscillating,
                limit_w=ctrl.last_osc_limit if ctrl.is_oscillating else None,
                holder_active=ctrl.holder is not None,
                predictor_active=ctrl.predictor is not None,
                holder_oscillating=ctrl.holder is not None and ctrl.holder.is_oscillating,
                predictor_oscillating=ctrl.predictor is not None and ctrl.predictor.is_oscillating,
            )
        ff = self._ff.get(phase)
        if ff is None:
            return OscState()
        return OscState(
            oscillating=ff.is_oscillating,
            limit_w=ff.last_osc_limit if ff.is_oscillating else None,
            holder_active=ff.holder is not None,
            predictor_active=ff.predictor is not None,
            holder_oscillating=ff.holder is not None and ff.holder.is_oscillating,
            predictor_oscillating=ff.predictor is not None and ff.predictor.is_oscillating,
        )


# ── Dynamic settings schema helper ───────────────────────────────────────────


def _phase_schema(ph_name: str, ph_cfg) -> dict[str, Any]:
    """Build flat settings schema entries for one phase."""
    p = ph_name.lower() + "_"
    is_fb = isinstance(ph_cfg, FeedbackPhaseConfig)
    group = f"Phase {ph_name} ({'Regulation' if is_fb else 'Steering'})"
    entries: dict[str, Any] = {}

    if is_fb:
        entries[p + "feedback_enabled"] = {
            "type": "boolean",
            "title": "Feedback enabled",
            "default": True,
            "group": group,
        }
        entries[p + "kp_draw"] = {
            "type": "number",
            "title": "Kp draw",
            "default": 0.9,
            "minimum": 0.0,
            "maximum": 5.0,
            "step": 0.05,
            "group": group,
        }
        entries[p + "kp_feed_in"] = {
            "type": "number",
            "title": "Kp feed-in",
            "default": 1.05,
            "minimum": 0.0,
            "maximum": 5.0,
            "step": 0.05,
            "group": group,
        }
    else:
        entries[p + "kp"] = {
            "type": "number",
            "title": "Kp",
            "default": 1.0,
            "minimum": 0.0,
            "maximum": 5.0,
            "step": 0.05,
            "group": group,
        }

    entries[p + "kp_hysteresis"] = {
        "type": "number",
        "title": "Kp hysteresis",
        "default": 0.4,
        "minimum": 0.0,
        "maximum": 2.0,
        "step": 0.05,
        "group": group,
    }
    entries[p + "hysteresis_w"] = {
        "type": "number",
        "title": "Hysteresis band [W]",
        "default": 5.0,
        "minimum": 0.0,
        "maximum": 100.0,
        "step": 0.5,
        "group": group,
    }
    entries[p + "holder_enabled"] = {
        "type": "boolean",
        "title": "Holder active (fast oscillations)",
        "default": False,
        "group": group,
    }
    entries[p + "holder_min_amplitude"] = {
        "type": "number",
        "title": "Holder min. amplitude [W]",
        "default": 30.0,
        "minimum": 5.0,
        "maximum": 500.0,
        "step": 5.0,
        "group": group,
    }
    entries[p + "predictor_enabled"] = {
        "type": "boolean",
        "title": "Predictor active (periodic loads)",
        "default": True,
        "group": group,
    }
    entries[p + "predictor_min_amplitude"] = {
        "type": "number",
        "title": "Predictor min. amplitude [W]",
        "default": 100.0,
        "minimum": 10.0,
        "maximum": 1000.0,
        "step": 10.0,
        "group": group,
    }
    return entries


# ── Regulator ─────────────────────────────────────────────────────────────────


class ZeroFeedV4Regulator(RegulatorBase):
    """ZeroFeed V4 – configurable-phase FF+FB zero-feed-in controller.

    ``control_phase`` selects which phase carries the battery inverter.
    All other phases are controlled via feedforward steering.
    Cycle-based watchdog resets only phases with persistent individual feed-in.
    Settings are persisted to YAML (ruamel.yaml, comments preserved).
    """

    _NAME = "zerofeed_v4"
    _DESC = (
        "V4: Configurable regulation phase (battery phase). "
        "All other phases: feedforward steering. "
        "Cycle-based watchdog, per-phase individual settings, YAML-persistent."
    )

    def __init__(
        self,
        settings: Optional[ZeroFeedV4Config] = None,
        yaml_path: Optional[Path] = None,
    ) -> None:
        self._yaml_path = yaml_path

        # Load from YAML if available, else use provided/default settings
        loaded = load_config(yaml_path) if yaml_path is not None else None
        cfg = loaded if loaded is not None else (settings or ZeroFeedV4Config())

        self._cfg = cfg
        self._core = _V4Core(cfg)
        self._queue: asyncio.Queue[_Sample] = asyncio.Queue(maxsize=cfg.queue_size())
        self._current_setpoint: int = 0
        self._watchdog_total: int = 0
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

        # Core V4 calculation
        ff_outputs, fb_correction, ff_sum = self._core.calculate(
            phase_samples=phase_samples,
            batt_hist=batt_hist,
            current_battery_output_w=batt_now,
            battery_settled=battery_settled,
        )

        raw_target = ff_sum + fb_correction

        # Watchdog (uses last samples of each phase)
        last_values = {ph: phase_samples[ph][-1].value for ph in ("A", "B", "C")}
        reset_phases = self._core.check_watchdog(last_values, ff_sum)
        if reset_phases:
            self._watchdog_total += 1
            raw_target = float(min_output_w)

        # Apply runtime limits
        new_sp = int(round(max(float(min_output_w), min(float(max_output_w), raw_target))))

        changed = False
        if new_sp != self._current_setpoint:
            ok = await battery.set_ac_output_limit(new_sp)
            if ok:
                # Record timing for PT1 model BEFORE updating current_setpoint
                now = time.time()
                self._sp_prev_output_w = self._estimate_batt_at(now)
                self._sp_sent_at = now
                self._sp_target_w = float(new_sp)

                self._current_setpoint = new_sp
                changed = True
                logger.debug(
                    "V4 setpoint → %d W  (ff=%s  fb=%.0fW  ff_sum=%.0fW  ctrl=%s)",
                    new_sp,
                    {ph: f"{v:.0f}" for ph, v in ff_outputs.items()},
                    fb_correction,
                    ff_sum,
                    self._cfg.control_phase,
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
            watchdog_resets=self._watchdog_total,
        )

        return self._current_setpoint if changed else None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def reset(self) -> None:
        self._core.reset()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._current_setpoint = 0
        self._watchdog_total = 0
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
            "watchdog_cycles": {
                "type": "integer",
                "title": "Watchdog cycles",
                "default": 3,
                "minimum": 1,
                "maximum": 20,
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
        return config_to_flat(self._cfg)

    def apply_settings(self, data: dict[str, Any]) -> None:
        old_cp = self._cfg.control_phase
        new_cfg = flat_to_config(data, self._cfg)
        self._cfg = new_cfg
        self._core = _V4Core(new_cfg)
        self._queue = asyncio.Queue(maxsize=new_cfg.queue_size())
        # PT1 state is kept (setpoint timing remains valid after param change)

        if self._yaml_path is not None:
            save_config(
                self._yaml_path,
                new_cfg,
                old_control_phase=old_cp if old_cp != new_cfg.control_phase else None,
            )

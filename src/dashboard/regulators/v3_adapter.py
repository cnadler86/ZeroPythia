"""ZeroFeed V3 regulator adapter.

Wraps the ZeroFeedV3 phase-controller logic as a ``RegulatorBase``
implementation.  The adapter re-uses the ZeroFeedManager and per-phase
controllers from ``src.controller.phase_controller`` directly – it does NOT
start the ZeroFeedV3Controller's internal asyncio tasks.

Instead, the ``ControlRuntime`` drives the adapter:
  - ``add_sample()``    – every ~1 s
  - ``compute_setpoint()`` – every ~control_interval_s (default 3 s)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from math import ceil
from typing import Any, Optional

from src.controller.oscillation_detectorv2 import BaseloadHolderSettings, BaseloadPredictorSettings
from src.controller.phase_controller import (
    InverterPhaseController,
    InverterPhaseControllerSettings,
    PhaseController,
    PhaseControllerSettings,
    PhaseSample,
    ZeroFeedManager,
    ZeroFeedManagerSettings,
)
from src.dashboard.models import ControlStatus, GridSample, OscState
from src.dashboard.regulator import BatteryInverterProtocol, RegulatorBase

logger = logging.getLogger(__name__)


# ── Settings dataclass ────────────────────────────────────────────────────────


@dataclass
class V3RegulatorSettings:
    """Configurable settings for the ZeroFeedV3 regulator."""

    # Manager limits
    max_output_w: int = 800
    min_output_w: int = 20
    target_power_w: float = 1.0

    # P-Controller gains
    kp_draw: float = 0.9
    kp_feed_in: float = 1.05
    kp_ff: float = 1.0  # Feedforward gain (Phase A + C)

    # Hysteresis
    hysteresis_w: float = 10.0
    hysteresis_kp: float = 0.3

    # Feedback
    feedback_enabled: bool = True

    # Oscillation detection A+C (Holder + Predictor, both use global settings below)
    osc_ac_holder_enabled: bool = False
    osc_ac_predictor_enabled: bool = True

    # Oscillation detection B (Holder + Predictor)
    osc_b_holder_enabled: bool = False
    osc_b_predictor_enabled: bool = True

    # ── BaseloadHolder settings (global, same for all phases) ──
    holder_threshold: float = 30.0
    holder_min_period: float = 1.0
    holder_max_period: float = 10.0
    holder_period_variance: float = 1.2
    holder_time_threshold: float = 0.6
    holder_min_rising_count: int = 3

    # ── BaseloadPredictor settings (global, same for all phases) ──
    predictor_threshold: float = 100.0
    predictor_min_period: float = 8.0
    predictor_max_period: float = 120.0
    predictor_period_variance: float = 2.0
    predictor_time_threshold: float = 2.0
    predictor_min_rising_count: int = 3
    predictor_reaction_time: float = 4.0

    # Timing
    control_interval_s: float = 3.0
    sampling_interval_s: float = 1.0


# ── Adapter ───────────────────────────────────────────────────────────────────


class ZeroFeedV3Regulator(RegulatorBase):
    """ZeroFeed V3 regulator – phase-aware zero-feed-in control.

    Uses the existing ZeroFeedManager internally; the ControlRuntime drives
    the sample/control loops instead of the controller's own asyncio tasks.
    """

    _NAME = "zerofeed_v3"
    _DESC = (
        "Phasen-bewusste Nulleinspeisung (V3). "
        "Feedforward auf Phase A+C, optionaler Feedback auf Phase B. "
        "Oszillationserkennung pro Phase konfigurierbar."
    )

    def __init__(self, settings: Optional[V3RegulatorSettings] = None) -> None:
        self._cfg = settings or V3RegulatorSettings()
        self._manager = self._build_manager(self._cfg)
        self._queue: asyncio.Queue[_InternalSample] = asyncio.Queue(maxsize=self._queue_size())
        self._current_setpoint: int = 0
        self._last_status: ControlStatus = ControlStatus(regulator_name=self._NAME, setpoint_w=0)

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._NAME

    @property
    def description(self) -> str:
        return self._DESC

    # ── Sampling ──────────────────────────────────────────────────────────────

    async def add_sample(self, sample: GridSample) -> None:
        s = _InternalSample(
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

        # Drain sample queue
        phase_a: list[PhaseSample] = []
        phase_b: list[PhaseSample] = []
        phase_c: list[PhaseSample] = []
        total_osc: list[PhaseSample] = []
        batt_hist: list[float] = []
        last_batt: float = 0.0

        while not self._queue.empty():
            try:
                s = self._queue.get_nowait()
                phase_a.append(PhaseSample(timestamp=s.timestamp, value=s.phase_a))
                phase_b.append(PhaseSample(timestamp=s.timestamp, value=s.phase_b))
                phase_c.append(PhaseSample(timestamp=s.timestamp, value=s.phase_c))
                total_osc.append(
                    PhaseSample(
                        timestamp=s.timestamp,
                        value=s.phase_a + s.phase_b + s.phase_c + s.battery_output,
                    )
                )
                batt_hist.append(s.battery_output)
                if s.battery_output >= 0:
                    last_batt = s.battery_output
            except asyncio.QueueEmpty:
                break

        if not phase_b:
            return None

        # Settlement check
        settled = await battery.is_settled(use_cache=False)
        battery_settled = settled is not False

        # Fresh battery output (avoid stale sample lag)
        fresh = await battery.get_ac_output_power()
        batt_now = float(fresh) if fresh is not None else last_batt

        # Manager calculation
        target_w, dbg = self._manager.calculate_debug(
            phase_a_samples=phase_a,
            phase_b_samples=phase_b,
            phase_c_samples=phase_c,
            current_battery_output_w=batt_now,
            battery_settled=battery_settled,
            phase_battery_output_samples_w=batt_hist,
            total_osc_samples=total_osc,
        )

        # Apply runtime-provided limits
        new_sp = int(round(max(float(min_output_w), min(float(max_output_w), target_w))))

        changed = False
        if new_sp != self._current_setpoint:
            ok = await battery.set_ac_output_limit(new_sp)
            if ok:
                self._current_setpoint = new_sp
                changed = True
                logger.debug("V3 setpoint → %d W", new_sp)

        # Collect oscillation state for dashboard
        pa = self._manager._phase_a
        pb = self._manager._phase_b
        pc = self._manager._phase_c

        osc_a_lim = pa.get_osc_limit() if pa.is_oscillating else None
        osc_b_lim = pb.get_osc_limit() if pb.is_oscillating else None
        osc_c_lim = pc.get_osc_limit() if pc.is_oscillating else None
        tot_osc = self._manager.total_is_oscillating
        tot_lim = self._manager.get_total_osc_limit() if tot_osc else None

        self._last_status = ControlStatus(
            regulator_name=self._NAME,
            setpoint_w=self._current_setpoint,
            setpoint_changed=changed,
            raw_target_w=target_w,
            ff_output_w=dbg.ff_output_w,
            feedback_output_w=dbg.feedback_output_w,
            osc_limit_w=dbg.osc_limit_w if dbg.osc_limit_w < 1e8 else None,
            osc_a=OscState(oscillating=pa.is_oscillating, limit_w=osc_a_lim),
            osc_b=OscState(oscillating=pb.is_oscillating, limit_w=osc_b_lim),
            osc_c=OscState(oscillating=pc.is_oscillating, limit_w=osc_c_lim),
            osc_total=OscState(oscillating=tot_osc, limit_w=tot_lim),
        )

        return self._current_setpoint if changed else None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def reset(self) -> None:
        self._manager = self._build_manager(self._cfg)
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._current_setpoint = 0
        self._last_status = ControlStatus(regulator_name=self._NAME, setpoint_w=0)

    # ── Status ────────────────────────────────────────────────────────────────

    def get_control_status(self) -> ControlStatus:
        return self._last_status

    # ── Settings ──────────────────────────────────────────────────────────────

    def settings_schema(self) -> dict[str, Any]:
        return {
            # ── Regler ───────────────────────────────────────────────────────
            "kp_draw": {
                "type": "number",
                "title": "Kp Netzbezug",
                "default": 0.9,
                "minimum": 0.0,
                "maximum": 5.0,
                "step": 0.05,
                "group": "Regler",
            },
            "kp_feed_in": {
                "type": "number",
                "title": "Kp Einspeisung",
                "default": 1.05,
                "minimum": 0.0,
                "maximum": 5.0,
                "step": 0.05,
                "group": "Regler",
            },
            "kp_ff": {
                "type": "number",
                "title": "Kp Feedforward (A+C)",
                "default": 1.0,
                "minimum": 0.0,
                "maximum": 5.0,
                "step": 0.05,
                "group": "Regler",
            },
            "target_power_w": {
                "type": "number",
                "title": "Ziel-Bezug [W]",
                "default": 1.0,
                "minimum": -50.0,
                "maximum": 100.0,
                "step": 1.0,
                "group": "Regler",
            },
            "hysteresis_w": {
                "type": "number",
                "title": "Hysterese [W]",
                "default": 10.0,
                "minimum": 0.0,
                "maximum": 50.0,
                "step": 0.5,
                "group": "Regler",
            },
            "feedback_enabled": {
                "type": "boolean",
                "title": "Feedback Phase B",
                "default": True,
                "group": "Regler",
            },
            "control_interval_s": {
                "type": "number",
                "title": "Regelintervall [s]",
                "default": 3.0,
                "minimum": 1.0,
                "maximum": 30.0,
                "step": 0.5,
                "group": "Regler",
            },
            # ── Oszillationserkennung Aktivierung ────────────────────────────
            "osc_ac_holder_enabled": {
                "type": "boolean",
                "title": "Holder aktiv (Phase A+C)",
                "default": False,
                "group": "Holder (kurze Schwingungen)",
            },
            "osc_b_holder_enabled": {
                "type": "boolean",
                "title": "Holder aktiv (Phase B)",
                "default": False,
                "group": "Holder (kurze Schwingungen)",
            },
            "holder_threshold": {
                "type": "number",
                "title": "Schwellwert [W]",
                "default": 30.0,
                "minimum": 1.0,
                "maximum": 500.0,
                "step": 1.0,
                "group": "Holder (kurze Schwingungen)",
            },
            "holder_min_period": {
                "type": "number",
                "title": "Min. Periode [s]",
                "default": 1.0,
                "minimum": 0.5,
                "maximum": 60.0,
                "step": 0.5,
                "group": "Holder (kurze Schwingungen)",
            },
            "holder_max_period": {
                "type": "number",
                "title": "Max. Periode [s]",
                "default": 10.0,
                "minimum": 1.0,
                "maximum": 120.0,
                "step": 1.0,
                "group": "Holder (kurze Schwingungen)",
            },
            "holder_period_variance": {
                "type": "number",
                "title": "Perioden-Varianz",
                "default": 1.2,
                "minimum": 0.1,
                "maximum": 5.0,
                "step": 0.1,
                "group": "Holder (kurze Schwingungen)",
            },
            "holder_time_threshold": {
                "type": "number",
                "title": "Merge-Fenster [s]",
                "default": 0.6,
                "minimum": 0.1,
                "maximum": 5.0,
                "step": 0.1,
                "group": "Holder (kurze Schwingungen)",
            },
            "holder_min_rising_count": {
                "type": "number",
                "title": "Min. Flanken",
                "default": 3,
                "minimum": 2,
                "maximum": 10,
                "step": 1,
                "group": "Holder (kurze Schwingungen)",
            },
            # ── Predictor ────────────────────────────────────────────────────
            "osc_ac_predictor_enabled": {
                "type": "boolean",
                "title": "Predictor aktiv (Phase A+C)",
                "default": False,
                "group": "Predictor (periodische Lasten)",
            },
            "osc_b_predictor_enabled": {
                "type": "boolean",
                "title": "Predictor aktiv (Phase B)",
                "default": False,
                "group": "Predictor (periodische Lasten)",
            },
            "predictor_threshold": {
                "type": "number",
                "title": "Schwellwert [W]",
                "default": 100.0,
                "minimum": 1.0,
                "maximum": 1000.0,
                "step": 5.0,
                "group": "Predictor (periodische Lasten)",
            },
            "predictor_min_period": {
                "type": "number",
                "title": "Min. Periode [s]",
                "default": 8.0,
                "minimum": 1.0,
                "maximum": 300.0,
                "step": 1.0,
                "group": "Predictor (periodische Lasten)",
            },
            "predictor_max_period": {
                "type": "number",
                "title": "Max. Periode [s]",
                "default": 120.0,
                "minimum": 10.0,
                "maximum": 600.0,
                "step": 5.0,
                "group": "Predictor (periodische Lasten)",
            },
            "predictor_period_variance": {
                "type": "number",
                "title": "Perioden-Varianz",
                "default": 2.0,
                "minimum": 0.1,
                "maximum": 10.0,
                "step": 0.1,
                "group": "Predictor (periodische Lasten)",
            },
            "predictor_time_threshold": {
                "type": "number",
                "title": "Merge-Fenster [s]",
                "default": 2.0,
                "minimum": 0.1,
                "maximum": 10.0,
                "step": 0.1,
                "group": "Predictor (periodische Lasten)",
            },
            "predictor_min_rising_count": {
                "type": "number",
                "title": "Min. Flanken",
                "default": 3,
                "minimum": 2,
                "maximum": 10,
                "step": 1,
                "group": "Predictor (periodische Lasten)",
            },
            "predictor_reaction_time": {
                "type": "number",
                "title": "Reaktionszeit [s]",
                "default": 4.0,
                "minimum": 0.5,
                "maximum": 30.0,
                "step": 0.5,
                "group": "Predictor (periodische Lasten)",
            },
        }

    def get_current_settings(self) -> dict[str, Any]:
        c = self._cfg
        return {
            "kp_draw": c.kp_draw,
            "kp_feed_in": c.kp_feed_in,
            "kp_ff": c.kp_ff,
            "target_power_w": c.target_power_w,
            "hysteresis_w": c.hysteresis_w,
            "feedback_enabled": c.feedback_enabled,
            "control_interval_s": c.control_interval_s,
            # Holder
            "osc_ac_holder_enabled": c.osc_ac_holder_enabled,
            "osc_b_holder_enabled": c.osc_b_holder_enabled,
            "holder_threshold": c.holder_threshold,
            "holder_min_period": c.holder_min_period,
            "holder_max_period": c.holder_max_period,
            "holder_period_variance": c.holder_period_variance,
            "holder_time_threshold": c.holder_time_threshold,
            "holder_min_rising_count": c.holder_min_rising_count,
            # Predictor
            "osc_ac_predictor_enabled": c.osc_ac_predictor_enabled,
            "osc_b_predictor_enabled": c.osc_b_predictor_enabled,
            "predictor_threshold": c.predictor_threshold,
            "predictor_min_period": c.predictor_min_period,
            "predictor_max_period": c.predictor_max_period,
            "predictor_period_variance": c.predictor_period_variance,
            "predictor_time_threshold": c.predictor_time_threshold,
            "predictor_min_rising_count": c.predictor_min_rising_count,
            "predictor_reaction_time": c.predictor_reaction_time,
        }

    def apply_settings(self, data: dict[str, Any]) -> None:
        c = self._cfg
        # Regler
        _f = lambda k: c.__setattr__(k, float(data[k])) if k in data else None
        _b = lambda k: c.__setattr__(k, bool(data[k])) if k in data else None
        _i = lambda k: c.__setattr__(k, int(data[k])) if k in data else None
        for k in (
            "kp_draw",
            "kp_feed_in",
            "kp_ff",
            "target_power_w",
            "hysteresis_w",
            "control_interval_s",
            "holder_threshold",
            "holder_min_period",
            "holder_max_period",
            "holder_period_variance",
            "holder_time_threshold",
            "predictor_threshold",
            "predictor_min_period",
            "predictor_max_period",
            "predictor_period_variance",
            "predictor_time_threshold",
            "predictor_reaction_time",
        ):
            _f(k)
        for k in ("holder_min_rising_count", "predictor_min_rising_count"):
            _i(k)
        for k in (
            "feedback_enabled",
            "osc_ac_holder_enabled",
            "osc_b_holder_enabled",
            "osc_ac_predictor_enabled",
            "osc_b_predictor_enabled",
        ):
            _b(k)
        # Rebuild manager with updated settings
        self._manager = self._build_manager(c)
        self._queue = asyncio.Queue(maxsize=self._queue_size())

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _queue_size(self) -> int:
        return ceil(self._cfg.control_interval_s / self._cfg.sampling_interval_s) + 2

    @staticmethod
    def _build_manager(cfg: V3RegulatorSettings) -> ZeroFeedManager:
        holder_settings = BaseloadHolderSettings(
            threshold=cfg.holder_threshold,
            min_period=cfg.holder_min_period,
            max_period=cfg.holder_max_period,
            period_variance=cfg.holder_period_variance,
            time_threshold=cfg.holder_time_threshold,
            min_rising_count=cfg.holder_min_rising_count,
        )
        predictor_settings = BaseloadPredictorSettings(
            threshold=cfg.predictor_threshold,
            min_period=cfg.predictor_min_period,
            max_period=cfg.predictor_max_period,
            period_variance=cfg.predictor_period_variance,
            time_threshold=cfg.predictor_time_threshold,
            min_rising_count=cfg.predictor_min_rising_count,
            reaction_time=cfg.predictor_reaction_time,
        )

        holder_ac = holder_settings if cfg.osc_ac_holder_enabled else None
        predictor_ac = predictor_settings if cfg.osc_ac_predictor_enabled else None
        holder_b = holder_settings if cfg.osc_b_holder_enabled else None
        predictor_b = predictor_settings if cfg.osc_b_predictor_enabled else None

        phase_a = PhaseController(
            settings=PhaseControllerSettings(
                kp=cfg.kp_ff,
                hysteresis_w=cfg.hysteresis_w,
                kp_hysteresis=cfg.hysteresis_kp,
            ),
            holder_settings=holder_ac,
            predictor_settings=predictor_ac,
        )
        phase_b = InverterPhaseController(
            settings=InverterPhaseControllerSettings(
                kp_draw=cfg.kp_draw,
                kp_feed_in=cfg.kp_feed_in,
                hysteresis_w=cfg.hysteresis_w,
                kp_hysteresis=cfg.hysteresis_kp,
                target_power_w=cfg.target_power_w,
                feedback_enabled=cfg.feedback_enabled,
            ),
            holder_settings=holder_b,
            predictor_settings=predictor_b,
        )
        phase_c = PhaseController(
            settings=PhaseControllerSettings(
                kp=cfg.kp_ff,
                hysteresis_w=cfg.hysteresis_w,
                kp_hysteresis=cfg.hysteresis_kp,
            ),
            holder_settings=holder_ac,
            predictor_settings=predictor_ac,
        )
        return ZeroFeedManager(
            manager_settings=ZeroFeedManagerSettings(
                min_output_w=cfg.min_output_w,
                max_output_w=cfg.max_output_w,
                target_power_w=cfg.target_power_w,
            ),
            phase_a=phase_a,
            phase_b=phase_b,
            phase_c=phase_c,
        )


# ── Internal data class ───────────────────────────────────────────────────────


@dataclass
class _InternalSample:
    """Internal sample stored in the regulator queue."""

    timestamp: float
    phase_a: float
    phase_b: float
    phase_c: float
    battery_output: float

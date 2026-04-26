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

from src.controller.oscillation_detectorv2 import BaseloadHolderSettings
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
    target_power_w: float = 3.0

    # P-Controller gains
    kp_draw: float = 0.9
    kp_feed_in: float = 1.05
    kp_ff: float = 1.0  # Feedforward gain (Phase A + C)

    # Hysteresis
    hysteresis_w: float = 10.0
    hysteresis_kp: float = 0.3

    # Feedback
    feedback_enabled: bool = True

    # Oscillation detection A+C
    osc_ac_enabled: bool = False

    # Oscillation detection B
    osc_b_enabled: bool = False

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
            "kp_draw": {
                "type": "number",
                "title": "Kp Netzbezug",
                "default": 0.9,
                "minimum": 0.0,
                "maximum": 5.0,
                "step": 0.05,
            },
            "kp_feed_in": {
                "type": "number",
                "title": "Kp Einspeisung",
                "default": 1.05,
                "minimum": 0.0,
                "maximum": 5.0,
                "step": 0.05,
            },
            "kp_ff": {
                "type": "number",
                "title": "Kp Feedforward (A+C)",
                "default": 1.0,
                "minimum": 0.0,
                "maximum": 5.0,
                "step": 0.05,
            },
            "target_power_w": {
                "type": "number",
                "title": "Ziel-Bezug [W]",
                "default": 3.0,
                "minimum": -50.0,
                "maximum": 100.0,
                "step": 1.0,
            },
            "hysteresis_w": {
                "type": "number",
                "title": "Hysterese [W]",
                "default": 10.0,
                "minimum": 0.0,
                "maximum": 50.0,
                "step": 0.5,
            },
            "feedback_enabled": {
                "type": "boolean",
                "title": "Feedback Phase B",
                "default": True,
            },
            "osc_ac_enabled": {
                "type": "boolean",
                "title": "Oszillationserkennung Phase A+C",
                "default": False,
            },
            "osc_b_enabled": {
                "type": "boolean",
                "title": "Oszillationserkennung Phase B",
                "default": False,
            },
            "control_interval_s": {
                "type": "number",
                "title": "Regelintervall [s]",
                "default": 3.0,
                "minimum": 1.0,
                "maximum": 30.0,
                "step": 0.5,
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
            "osc_ac_enabled": c.osc_ac_enabled,
            "osc_b_enabled": c.osc_b_enabled,
            "control_interval_s": c.control_interval_s,
        }

    def apply_settings(self, data: dict[str, Any]) -> None:
        c = self._cfg
        if "kp_draw" in data:
            c.kp_draw = float(data["kp_draw"])
        if "kp_feed_in" in data:
            c.kp_feed_in = float(data["kp_feed_in"])
        if "kp_ff" in data:
            c.kp_ff = float(data["kp_ff"])
        if "target_power_w" in data:
            c.target_power_w = float(data["target_power_w"])
        if "hysteresis_w" in data:
            c.hysteresis_w = float(data["hysteresis_w"])
        if "feedback_enabled" in data:
            c.feedback_enabled = bool(data["feedback_enabled"])
        if "osc_ac_enabled" in data:
            c.osc_ac_enabled = bool(data["osc_ac_enabled"])
        if "osc_b_enabled" in data:
            c.osc_b_enabled = bool(data["osc_b_enabled"])
        if "control_interval_s" in data:
            c.control_interval_s = float(data["control_interval_s"])
        # Rebuild manager with updated settings
        self._manager = self._build_manager(c)
        self._queue = asyncio.Queue(maxsize=self._queue_size())

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _queue_size(self) -> int:
        return ceil(self._cfg.control_interval_s / self._cfg.sampling_interval_s) + 2

    @staticmethod
    def _build_manager(cfg: V3RegulatorSettings) -> ZeroFeedManager:
        holder_ac = BaseloadHolderSettings() if cfg.osc_ac_enabled else None
        holder_b = BaseloadHolderSettings() if cfg.osc_b_enabled else None

        phase_a = PhaseController(
            settings=PhaseControllerSettings(
                kp=cfg.kp_ff,
                hysteresis_w=cfg.hysteresis_w,
                kp_hysteresis=cfg.hysteresis_kp,
            ),
            holder_settings=holder_ac,
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
        )
        phase_c = PhaseController(
            settings=PhaseControllerSettings(
                kp=cfg.kp_ff,
                hysteresis_w=cfg.hysteresis_w,
                kp_hysteresis=cfg.hysteresis_kp,
            ),
            holder_settings=holder_ac,
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

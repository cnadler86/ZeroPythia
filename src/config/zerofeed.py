"""Pydantic v2 configuration for ZeroFeed.

Two controller types, configurable per phase:

  FeedforwardPhaseConfig  – open-loop steering for phases WITHOUT the battery.
                            P-control with target = 0 W.  No stability risk.

  FeedbackPhaseConfig     – closed-loop regulation for the phase WITH the battery.
                            Variable target = -(ff_sum) + global_target_w.

Exactly one phase carries role='feedback'; its name is stored in ``control_phase``.
All other phases carry role='feedforward'.

Oscillation detection reuses the existing ``BaseloadHolderSettings`` and
``BaseloadPredictorSettings`` dataclasses directly – no parallel config models.
``None`` means the detector is disabled; a settings object means enabled.

YAML persistence uses ruamel.yaml so hand-written comments in the config file
are preserved across dashboard-triggered saves.
"""

from __future__ import annotations

import logging
from io import StringIO
from math import ceil
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from src.controller.oscillation_detectorv2 import BaseloadHolderSettings, BaseloadPredictorSettings

logger = logging.getLogger(__name__)

# ── Oscillation detector config ───────────────────────────────────────────────


class OscillationConfig(BaseModel):
    """Oscillation detector settings for one phase.

    ``holder``    – ``None`` disables the holder;  a ``BaseloadHolderSettings``
                    object enables it with those parameters.
    ``predictor`` – same pattern; defaults to enabled with standard settings.
    """

    model_config = ConfigDict(validate_assignment=True)

    holder: Optional[BaseloadHolderSettings] = None
    predictor: Optional[BaseloadPredictorSettings] = Field(
        default_factory=BaseloadPredictorSettings
    )


# ── Per-phase configs (discriminated union) ───────────────────────────────────


class FeedforwardPhaseConfig(BaseModel):
    """Open-loop steering for a phase WITHOUT the battery inverter.

    P-control with target = 0 W.  Output is a battery demand request.
    No stability risk because the battery is on a different phase.
    """

    model_config = ConfigDict(validate_assignment=True)

    role: Literal["feedforward"] = "feedforward"

    kp: float = 1.0
    """P-gain outside hysteresis.  1.0 = full compensation of grid draw."""

    kp_hysteresis: float = 0.3
    """Damped gain inside hysteresis band."""

    hysteresis_w: float = 10.0
    """Hysteresis band [W].  Inside: kp_hysteresis active.  Outside: kp active."""

    osc: OscillationConfig = Field(default_factory=OscillationConfig)


class FeedbackPhaseConfig(BaseModel):
    """Closed-loop regulation for the phase WITH the battery inverter.

    P-controller with variable target = -(ff_sum) + global_target_w.
    Oscillation detection runs on the estimated real load of this phase:
        real_load ≈ phase_grid + battery_output - ff_sum
    This limit applies only to this phase; feedforward phases are
    independently protected by their own oscillation detectors.
    """

    model_config = ConfigDict(validate_assignment=True)

    role: Literal["feedback"] = "feedback"

    kp_draw: float = 0.9
    """P-gain when drawing from grid (conservative)."""

    kp_feed_in: float = 1.05
    """P-gain when feeding into grid (more aggressive to pull back)."""

    kp_hysteresis: float = 0.3
    """Damped gain inside hysteresis band."""

    hysteresis_w: float = 10.0
    """Hysteresis band [W]."""

    feedback_enabled: bool = True
    """False = pure feedforward mode: battery phase contributes 0, total = ff_sum.
    Useful for testing or if the battery phase has no independent load."""

    osc: OscillationConfig = Field(default_factory=OscillationConfig)


# Pydantic v2 discriminated union – type-safe deserialization
PhaseConfig = Annotated[
    FeedforwardPhaseConfig | FeedbackPhaseConfig,
    Field(discriminator="role"),
]

# ── Main config ───────────────────────────────────────────────────────────────

_ALL_PHASES = ("A", "B", "C")


class ZeroFeedConfig(BaseModel):
    """Complete configuration for the ZeroFeed regulator.

    ``control_phase`` names the phase with the battery inverter.
    All three phases (A, B, C) must have entries in ``phases``; missing ones
    are filled with default configs by the model validator.
    """

    model_config = ConfigDict(validate_assignment=True)

    control_phase: Literal["A", "B", "C"] = "B"
    """Phase to which the battery inverter is connected (feedback / regulation phase).
    All other phases use feedforward / steering control."""

    target_power_w: float = 3.0
    """Desired total grid draw [W].  3 W = slight import, prevents feed-in."""

    min_output_w: int = 20
    """Minimum battery output [W] (hardware limit)."""

    max_output_w: int = 800
    """Maximum battery output [W] (hardware limit)."""

    battery_dead_time_s: float = 1.1
    """Dead time [s] from setpoint command to first battery response at the grid meter."""

    battery_pt1_tau_s: float = 0.5
    """PT1 time constant [s] for battery output ramp-up/ramp-down model."""

    watchdog_cycles: int = 3
    """Consecutive control cycles with feed-in outside hysteresis before the
    watchdog resets only the affected phase controller(s)."""

    control_interval_s: float = 3.0
    """Control cycle interval [s]."""

    sampling_interval_s: float = 1.0
    """Sampling interval [s]."""

    language: str = "en"
    """Dashboard UI language.  Supported: 'en' (English), 'de' (German)."""

    phases: dict[str, PhaseConfig] = Field(default_factory=dict)
    """Per-phase controller configs keyed by phase letter ('A', 'B', 'C').
    Populated automatically with defaults for any missing phase."""

    # ── Validator ─────────────────────────────────────────────────────────────

    @model_validator(mode="after")
    def _fill_and_validate_phases(self) -> ZeroFeedConfig:
        # Fill missing phases with appropriate defaults
        for ph in _ALL_PHASES:
            if ph not in self.phases:
                self.phases[ph] = (
                    FeedbackPhaseConfig() if ph == self.control_phase else FeedforwardPhaseConfig()
                )

        # Exactly one feedback phase, and it must match control_phase
        feedback_phases = [
            ph for ph, cfg in self.phases.items() if isinstance(cfg, FeedbackPhaseConfig)
        ]
        if len(feedback_phases) > 1:
            raise ValueError(f"Only one phase may have role='feedback', found: {feedback_phases}")
        ctrl = self.phases.get(self.control_phase)
        if ctrl is not None and not isinstance(ctrl, FeedbackPhaseConfig):
            raise ValueError(
                f"Phase {self.control_phase!r} is control_phase but has role={ctrl.role!r}; "
                "it must have role='feedback'."
            )
        return self

    # ── Derived helpers ────────────────────────────────────────────────────────

    def queue_size(self) -> int:
        return ceil(self.control_interval_s / self.sampling_interval_s) + 1

    def feedforward_phases(self) -> list[str]:
        """Sorted list of feedforward (steering) phase names."""
        return sorted(
            ph for ph, cfg in self.phases.items() if isinstance(cfg, FeedforwardPhaseConfig)
        )

    def feedback_phase(self) -> str:
        """The feedback (regulation) phase name (== control_phase)."""
        return self.control_phase


# ── Flat settings bridge (for dashboard API) ──────────────────────────────────


def config_to_flat(cfg: ZeroFeedConfig) -> dict[str, Any]:
    """Serialize config to a flat ``{key: value}`` dict for the dashboard API."""
    holder_defaults = BaseloadHolderSettings()
    predictor_defaults = BaseloadPredictorSettings()

    result: dict[str, Any] = {
        "control_phase": cfg.control_phase,
        "target_power_w": cfg.target_power_w,
        "watchdog_cycles": cfg.watchdog_cycles,
        "control_interval_s": cfg.control_interval_s,
        "battery_dead_time_s": cfg.battery_dead_time_s,
        "battery_pt1_tau_s": cfg.battery_pt1_tau_s,
    }
    for ph in _ALL_PHASES:
        ph_cfg = cfg.phases.get(ph)
        if ph_cfg is None:
            continue
        p = ph.lower() + "_"
        if isinstance(ph_cfg, FeedbackPhaseConfig):
            result[p + "feedback_enabled"] = ph_cfg.feedback_enabled
            result[p + "kp_draw"] = ph_cfg.kp_draw
            result[p + "kp_feed_in"] = ph_cfg.kp_feed_in
        else:
            result[p + "kp"] = ph_cfg.kp
        result[p + "kp_hysteresis"] = ph_cfg.kp_hysteresis
        result[p + "hysteresis_w"] = ph_cfg.hysteresis_w
        holder = ph_cfg.osc.holder
        predictor = ph_cfg.osc.predictor

        result[p + "holder_enabled"] = holder is not None
        result[p + "holder_min_amplitude"] = (
            holder.threshold if holder is not None else holder_defaults.threshold
        )
        result[p + "holder_min_period"] = (
            holder.min_period if holder is not None else holder_defaults.min_period
        )
        result[p + "holder_max_period"] = (
            holder.max_period if holder is not None else holder_defaults.max_period
        )
        result[p + "holder_period_variance"] = (
            holder.period_variance if holder is not None else holder_defaults.period_variance
        )
        result[p + "holder_time_threshold"] = (
            holder.time_threshold if holder is not None else holder_defaults.time_threshold
        )
        result[p + "holder_min_rising_count"] = (
            holder.min_rising_count if holder is not None else holder_defaults.min_rising_count
        )
        result[p + "holder_merge_mode"] = (
            holder.merge_mode if holder is not None else holder_defaults.merge_mode
        )
        result[p + "holder_base_load_window"] = (
            holder.base_load_window if holder is not None else holder_defaults.base_load_window
        )

        result[p + "predictor_enabled"] = predictor is not None
        result[p + "predictor_min_amplitude"] = (
            predictor.threshold if predictor is not None else predictor_defaults.threshold
        )
        result[p + "predictor_min_period"] = (
            predictor.min_period if predictor is not None else predictor_defaults.min_period
        )
        result[p + "predictor_max_period"] = (
            predictor.max_period if predictor is not None else predictor_defaults.max_period
        )
        result[p + "predictor_period_variance"] = (
            predictor.period_variance
            if predictor is not None
            else predictor_defaults.period_variance
        )
        result[p + "predictor_time_threshold"] = (
            predictor.time_threshold if predictor is not None else predictor_defaults.time_threshold
        )
        result[p + "predictor_min_rising_count"] = (
            predictor.min_rising_count
            if predictor is not None
            else predictor_defaults.min_rising_count
        )
        result[p + "predictor_merge_mode"] = (
            predictor.merge_mode if predictor is not None else predictor_defaults.merge_mode
        )
        result[p + "predictor_base_load_window"] = (
            predictor.base_load_window
            if predictor is not None
            else predictor_defaults.base_load_window
        )
        result[p + "predictor_reaction_time"] = (
            predictor.reaction_time if predictor is not None else predictor_defaults.reaction_time
        )
    return result


def flat_to_config(data: dict[str, Any], base: ZeroFeedConfig) -> ZeroFeedConfig:
    """Apply a flat settings dict on top of an existing config, return new validated config.

    If ``control_phase`` changes, the roles of the involved phases are swapped;
    existing per-phase tuning values are preserved where the role is unchanged.
    """
    raw = base.model_dump()

    # ── Global fields ──────────────────────────────────────────────────────────
    for key in (
        "target_power_w",
        "watchdog_cycles",
        "control_interval_s",
        "sampling_interval_s",
        "battery_dead_time_s",
        "battery_pt1_tau_s",
    ):
        if key in data:
            raw[key] = data[key]

    # ── control_phase change → swap roles ─────────────────────────────────────
    new_cp = str(data.get("control_phase", raw["control_phase"]))
    old_cp = raw["control_phase"]
    if new_cp != old_cp and new_cp in _ALL_PHASES:
        raw["control_phase"] = new_cp
        phases = raw["phases"]
        # Old feedback → feedforward (keep osc, drop fb-specific keys)
        old_fb = phases.get(old_cp, {})
        phases[old_cp] = {
            "role": "feedforward",
            "kp": 1.0,
            "kp_hysteresis": old_fb.get("kp_hysteresis", 0.3),
            "hysteresis_w": old_fb.get("hysteresis_w", 10.0),
            "osc": old_fb.get("osc", {}),
        }
        # New feedback ← feedforward (keep osc, add fb-specific keys with defaults)
        new_fb = phases.get(new_cp, {})
        phases[new_cp] = {
            "role": "feedback",
            "kp_draw": 0.9,
            "kp_feed_in": 1.05,
            "kp_hysteresis": new_fb.get("kp_hysteresis", 0.3),
            "hysteresis_w": new_fb.get("hysteresis_w", 10.0),
            "feedback_enabled": True,
            "osc": new_fb.get("osc", {}),
        }

    # ── Per-phase fields ───────────────────────────────────────────────────────
    def _ensure_osc_dict(osc: dict[str, Any], key: str) -> dict[str, Any]:
        current = osc.get(key)
        if not isinstance(current, dict):
            current = {}
            osc[key] = current
        return current

    for ph in _ALL_PHASES:
        ph_raw = raw["phases"].get(ph)
        if ph_raw is None:
            continue
        p = ph.lower() + "_"
        role = ph_raw.get("role", "feedforward")
        if role == "feedback":
            for field in ("kp_draw", "kp_feed_in", "feedback_enabled"):
                if p + field in data:
                    ph_raw[field] = data[p + field]
        else:
            if p + "kp" in data:
                ph_raw["kp"] = data[p + "kp"]
        for field in ("kp_hysteresis", "hysteresis_w"):
            if p + field in data:
                ph_raw[field] = data[p + field]
        osc = ph_raw.setdefault("osc", {})
        if p + "holder_enabled" in data:
            if data[p + "holder_enabled"]:
                # Enable: keep existing settings or use defaults
                if osc.get("holder") is None:
                    osc["holder"] = {}  # triggers BaseloadHolderSettings defaults
            else:
                osc["holder"] = None
        # Holder min amplitude (threshold)
        if p + "holder_min_amplitude" in data and osc.get("holder") is not None:
            holder = _ensure_osc_dict(osc, "holder")
            holder["threshold"] = data[p + "holder_min_amplitude"]
        for src, target in (
            ("holder_min_period", "min_period"),
            ("holder_max_period", "max_period"),
            ("holder_period_variance", "period_variance"),
            ("holder_time_threshold", "time_threshold"),
            ("holder_min_rising_count", "min_rising_count"),
            ("holder_merge_mode", "merge_mode"),
            ("holder_base_load_window", "base_load_window"),
        ):
            key = p + src
            if key in data and osc.get("holder") is not None:
                holder = _ensure_osc_dict(osc, "holder")
                holder[target] = data[key]

        if p + "predictor_enabled" in data:
            if data[p + "predictor_enabled"]:
                if osc.get("predictor") is None:
                    osc["predictor"] = {}  # triggers BaseloadPredictorSettings defaults
            else:
                osc["predictor"] = None
        # Predictor min amplitude (threshold)
        if p + "predictor_min_amplitude" in data and osc.get("predictor") is not None:
            predictor = _ensure_osc_dict(osc, "predictor")
            predictor["threshold"] = data[p + "predictor_min_amplitude"]
        for src, target in (
            ("predictor_min_period", "min_period"),
            ("predictor_max_period", "max_period"),
            ("predictor_period_variance", "period_variance"),
            ("predictor_time_threshold", "time_threshold"),
            ("predictor_min_rising_count", "min_rising_count"),
            ("predictor_merge_mode", "merge_mode"),
            ("predictor_base_load_window", "base_load_window"),
            ("predictor_reaction_time", "reaction_time"),
        ):
            key = p + src
            if key in data and osc.get("predictor") is not None:
                predictor = _ensure_osc_dict(osc, "predictor")
                predictor[target] = data[key]

    return ZeroFeedConfig.model_validate(raw)


# ── YAML I/O with comment support ─────────────────────────────────────────────


def _make_yaml_instance() -> YAML:
    y = YAML()
    y.default_flow_style = False
    y.allow_unicode = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 100
    return y


def _build_commented_map(cfg: ZeroFeedConfig) -> CommentedMap:
    """Build a ruamel.yaml CommentedMap with descriptive inline comments (English)."""
    d = CommentedMap()
    d.yaml_set_start_comment(
        "ZeroFeed configuration\n"
        "Exactly one phase has role: feedback (regulation phase = battery phase).\n"
        "All other phases have role: feedforward (steering phase, no battery inverter).\n"
        "This file is updated automatically by the dashboard; comments are preserved.\n"
    )

    d["control_phase"] = cfg.control_phase
    d.yaml_add_eol_comment("Phase with battery inverter (A, B or C)", "control_phase")

    d["target_power_w"] = cfg.target_power_w
    d.yaml_add_eol_comment(
        "Desired total grid draw [W].  3 W = slight import, prevents feed-in", "target_power_w"
    )

    d["min_output_w"] = cfg.min_output_w
    d.yaml_add_eol_comment("Minimum battery output [W] (hardware limit, e.g. 20 W)", "min_output_w")

    d["max_output_w"] = cfg.max_output_w
    d.yaml_add_eol_comment(
        "Maximum battery output [W] (hardware limit, e.g. 800 W)", "max_output_w"
    )

    d["watchdog_cycles"] = cfg.watchdog_cycles
    d.yaml_add_eol_comment(
        "Consecutive cycles with feed-in outside hysteresis → reset affected phases",
        "watchdog_cycles",
    )

    d["control_interval_s"] = cfg.control_interval_s
    d.yaml_add_eol_comment("Control cycle interval [s]", "control_interval_s")

    d["sampling_interval_s"] = cfg.sampling_interval_s
    d.yaml_add_eol_comment("Sampling interval [s]", "sampling_interval_s")

    # Phases
    phases_map = CommentedMap()
    for ph_name in ("A", "B", "C"):
        ph_cfg = cfg.phases.get(ph_name)
        if ph_cfg is None:
            continue

        is_fb = isinstance(ph_cfg, FeedbackPhaseConfig)
        role_label = "Regulation – battery inverter" if is_fb else "Steering – no battery inverter"
        phases_map.yaml_set_comment_before_after_key(
            ph_name, before=f"\nPhase {ph_name} ({role_label})"
        )

        ph_map = CommentedMap()
        ph_map["role"] = ph_cfg.role
        ph_map.yaml_add_eol_comment(
            "feedback = regulation (battery phase)  |  feedforward = steering", "role"
        )

        if isinstance(ph_cfg, FeedbackPhaseConfig):
            ph_map["kp_draw"] = ph_cfg.kp_draw
            ph_map.yaml_add_eol_comment("P-gain when drawing from grid", "kp_draw")
            ph_map["kp_feed_in"] = ph_cfg.kp_feed_in
            ph_map.yaml_add_eol_comment(
                "P-gain when feeding into grid (higher = more aggressive pull-back)", "kp_feed_in"
            )
            ph_map["feedback_enabled"] = ph_cfg.feedback_enabled
            ph_map.yaml_add_eol_comment(
                "false = pure feedforward mode (for testing)", "feedback_enabled"
            )
        else:
            ph_map["kp"] = ph_cfg.kp
            ph_map.yaml_add_eol_comment(
                "P-gain outside hysteresis band.  1.0 = full compensation", "kp"
            )

        ph_map["kp_hysteresis"] = ph_cfg.kp_hysteresis
        ph_map.yaml_add_eol_comment("Damped gain inside hysteresis band", "kp_hysteresis")
        ph_map["hysteresis_w"] = ph_cfg.hysteresis_w
        ph_map.yaml_add_eol_comment(
            "Hysteresis band [W].  Inside: kp_hysteresis active  Outside: kp / kp_draw",
            "hysteresis_w",
        )

        # Oscillation sub-section
        osc = ph_cfg.osc
        ph_map.yaml_set_comment_before_after_key("osc", before="  Oscillation detectors")
        osc_map = CommentedMap()

        # -- Holder sub-map (None = disabled) --
        osc_map.yaml_set_comment_before_after_key(
            "holder", before="    Holder – fast short-cycle oscillations (period < ~10 s)"
        )
        h = osc.holder
        if h is None:
            osc_map["holder"] = None
            osc_map.yaml_add_eol_comment("null = disabled", "holder")
        else:
            h_map = CommentedMap()
            h_map["threshold"] = h.threshold
            h_map.yaml_add_eol_comment("Minimum amplitude [W] to confirm oscillation", "threshold")
            h_map["min_period"] = h.min_period
            h_map.yaml_add_eol_comment("Shortest detectable period [s]", "min_period")
            h_map["max_period"] = h.max_period
            h_map.yaml_add_eol_comment("Longest detectable period [s]", "max_period")
            h_map["period_variance"] = h.period_variance
            h_map.yaml_add_eol_comment(
                "Allowed jitter factor for period matching", "period_variance"
            )
            h_map["time_threshold"] = h.time_threshold
            h_map.yaml_add_eol_comment(
                "Minimum duty cycle (0..1) – fraction of cycle the load must be active",
                "time_threshold",
            )
            h_map["min_rising_count"] = h.min_rising_count
            h_map.yaml_add_eol_comment(
                "Minimum rising edges required to confirm oscillation", "min_rising_count"
            )
            osc_map["holder"] = h_map

        # -- Predictor sub-map (None = disabled) --
        osc_map.yaml_set_comment_before_after_key(
            "predictor",
            before="    Predictor – periodic loads with known cycle time (e.g. washing machine)",
        )
        p = osc.predictor
        if p is None:
            osc_map["predictor"] = None
            osc_map.yaml_add_eol_comment("null = disabled", "predictor")
        else:
            p_map = CommentedMap()
            p_map["threshold"] = p.threshold
            p_map.yaml_add_eol_comment(
                "Minimum amplitude [W] to confirm periodic load", "threshold"
            )
            p_map["min_period"] = p.min_period
            p_map.yaml_add_eol_comment("Shortest detectable period [s]", "min_period")
            p_map["max_period"] = p.max_period
            p_map.yaml_add_eol_comment("Longest detectable period [s]", "max_period")
            p_map["period_variance"] = p.period_variance
            p_map.yaml_add_eol_comment(
                "Allowed jitter factor for period matching", "period_variance"
            )
            p_map["time_threshold"] = p.time_threshold
            p_map.yaml_add_eol_comment(
                "Minimum duty cycle to confirm periodic load", "time_threshold"
            )
            p_map["min_rising_count"] = p.min_rising_count
            p_map.yaml_add_eol_comment(
                "Minimum rising edges required to confirm periodic load", "min_rising_count"
            )
            p_map["reaction_time"] = p.reaction_time
            p_map.yaml_add_eol_comment(
                "Lead time [s] before expected load peak – battery output reduced early",
                "reaction_time",
            )
            osc_map["predictor"] = p_map

        ph_map["osc"] = osc_map
        phases_map[ph_name] = ph_map

    d["phases"] = phases_map
    d.yaml_set_comment_before_after_key("phases", before="\nPer-phase controller settings")
    return d


def _update_inplace(target: Any, source: dict) -> None:
    """Recursively update a ruamel.yaml CommentedMap in-place (preserves comments)."""
    for key, value in source.items():
        if isinstance(value, dict) and key in target and hasattr(target[key], "keys"):
            _update_inplace(target[key], value)
        else:
            target[key] = value


# ── Public I/O functions ──────────────────────────────────────────────────────


def load_config(path: Path) -> Optional[ZeroFeedConfig]:
    """Load ZeroFeedConfig from a YAML file.  Returns None on missing file or parse error."""
    if not path.exists():
        return None
    try:
        y = _make_yaml_instance()
        raw = y.load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return ZeroFeedConfig.model_validate(raw)
    except Exception as exc:
        logger.warning("ZeroFeed config: Fehler beim Laden von %s: %s", path, exc)
    return None


def save_config(
    path: Path, cfg: ZeroFeedConfig, *, old_control_phase: Optional[str] = None
) -> None:
    """Save ZeroFeedConfig to a YAML file, preserving existing comments.

    If the file exists, values are updated in-place so user-added comments survive.
    If the phases structure changed (control_phase swap), the ``phases`` section
    is regenerated with fresh comments.
    If the file does not exist, a new commented YAML is generated.

    Args:
        path:              Target YAML file path.
        cfg:               Config to persist.
        old_control_phase: Previous control phase, if it just changed.
    """
    y = _make_yaml_instance()
    phase_changed = old_control_phase is not None and old_control_phase != cfg.control_phase

    if path.exists() and not phase_changed:
        # Load existing file (preserves user comments), update values in-place
        try:
            data = y.load(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _update_inplace(data, cfg.model_dump())
            else:
                data = _build_commented_map(cfg)
        except Exception as exc:
            logger.warning(
                "ZeroFeed config: Fehler beim Lesen für In-Place-Update (%s): %s", path, exc
            )
            data = _build_commented_map(cfg)
    elif path.exists() and phase_changed:
        # Phase structure changed – regenerate phases section, preserve rest
        try:
            data = y.load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("unexpected YAML root type")
            data["control_phase"] = cfg.control_phase
            new_phases = _build_commented_map(cfg)["phases"]
            # Clear any pre-key comments that ruamel attached to the old phases value
            # to avoid duplicate header comments after a phase-role swap.
            if hasattr(new_phases, "ca"):
                new_phases.ca.items.clear()
            data["phases"] = new_phases
            top_level_dump = {
                k: v for k, v in cfg.model_dump().items() if k not in ("phases", "control_phase")
            }
            _update_inplace(data, top_level_dump)
        except Exception as exc:
            logger.warning(
                "ZeroFeed config: Fehler beim Lesen für Phase-Swap-Update (%s): %s", path, exc
            )
            data = _build_commented_map(cfg)
    else:
        # New file – generate with comments
        data = _build_commented_map(cfg)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = StringIO()
        y.dump(data, stream)
        path.write_text(stream.getvalue(), encoding="utf-8")
        logger.info("ZeroFeed config: gespeichert → %s", path)
    except Exception as exc:
        logger.error("ZeroFeed config: Fehler beim Speichern (%s): %s", path, exc)

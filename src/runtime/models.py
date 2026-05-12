"""Runtime domain models shared by control and dashboard layers.

These models are owned by the runtime/control domain and can be consumed by
UI-facing packages such as ``src.dashboard``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class DeviceMode(str, Enum):
    """Operating modes of the battery inverter."""

    AC_CHARGE = "ac_charge"
    IDLE = "idle"
    DISCHARGE_ZERO_FEED = "discharge_zero_feed"
    AUTO = "auto"


class ZFIState(str, Enum):
    """Operational state of the Zero-Feed-In regulation loop.

    Replaces the five separate boolean ZFI-pause flags previously held by
    ``ControlRuntime``.  A single enum value captures the full state, making
    transitions explicit and eliminating impossible flag combinations.
    """

    INACTIVE = "inactive"  # Not in ZFI mode (IDLE / AC_CHARGE / AUTO-idle)
    RUNNING = "running"  # Regulator active, full power budget available
    SOFT_LIMITED = "soft_limited"  # Regulator active, output capped (SoC hysteresis)
    PAUSED_LOW_SOC = "paused_low_soc"  # Battery stopped: SoC too low, no PV
    PAUSED_FULL = "paused_full"  # Battery stopped: battery fully charged
    PAUSED_NO_GRID = "paused_no_grid"  # Regulator suspended: no Shelly / grid-meter data


class GridSample(BaseModel):
    """One measurement snapshot from Shelly + Zendure."""

    timestamp: float = Field(description="Unix timestamp (seconds).")
    phase_a_w: float = Field(description="Grid power on phase A [W].")
    phase_b_w: float = Field(description="Grid power on phase B [W].")
    phase_c_w: float = Field(description="Grid power on phase C [W].")
    battery_output_w: float = Field(description="Battery AC output [W].")
    soc_percent: Optional[int] = Field(default=None, description="Battery SoC [%].")
    charge_input_w: Optional[float] = Field(
        default=None, description="Battery AC charge input [W]."
    )
    solar_input_w: Optional[float] = Field(
        default=None, description="Solar PV input power reported by the inverter [W]."
    )
    bypass_active: Optional[bool] = Field(
        default=None,
        description="True when inverter is in bypass mode (PV direct to house).",
    )

    @property
    def total_grid_w(self) -> float:
        return self.phase_a_w + self.phase_b_w + self.phase_c_w

    @property
    def real_consumption_w(self) -> float:
        return self.total_grid_w + self.battery_output_w


class OscState(BaseModel):
    oscillating: bool = False
    limit_w: Optional[float] = None
    holder_active: bool = False
    predictor_active: bool = False
    holder_oscillating: bool = False
    predictor_oscillating: bool = False


class ControlStatus(BaseModel):
    regulator_name: str
    setpoint_w: int = Field(description="Final setpoint sent to battery [W].")
    setpoint_changed: bool = False
    raw_target_w: Optional[float] = None
    target_power_w: float = 0.0
    ff_output_w: Optional[float] = None
    feedback_output_w: Optional[float] = None
    ff_per_phase: dict[str, float] = Field(default_factory=dict)
    osc_limit_w: Optional[float] = None
    osc_a: OscState = Field(default_factory=OscState)
    osc_b: OscState = Field(default_factory=OscState)
    osc_c: OscState = Field(default_factory=OscState)
    osc_total: OscState = Field(default_factory=OscState)
    watchdog_resets: int = 0


class RegulatorInfo(BaseModel):
    name: str
    description: str = ""
    is_active: bool = False
    settings_schema: dict[str, Any] = Field(default_factory=dict)
    current_settings: dict[str, Any] = Field(default_factory=dict)


class PlanSummaryEntry(BaseModel):
    mode_label: str = Field(description="Human-readable mode name.")
    from_time: str = Field(description="Start time HH:MM (local).")
    to_time: str = Field(description="End time HH:MM (local).")
    power_w: Optional[int] = Field(default=None, description="Power [W].")
    date: Optional[str] = Field(default=None, description="Day label when not today.")
    end_next_day: bool = Field(default=False)


class AutoStatus(BaseModel):
    connected: bool = False
    has_plan: bool = False
    plan_published_at: Optional[str] = None
    plan_received_at: Optional[str] = None
    effective_mode: str = "-"
    plan_summary: list[PlanSummaryEntry] = Field(default_factory=list)


class DashboardState(BaseModel):
    timestamp: float
    mode: DeviceMode
    charge_power_w: Optional[int] = Field(default=None)
    max_discharge_w: int = Field(default=800)
    active_regulator: Optional[str] = None
    sample: Optional[GridSample] = None
    control: Optional[ControlStatus] = None
    auto_status: Optional[AutoStatus] = None
    zfi_state: ZFIState = ZFIState.INACTIVE
    zfi_soc_limit_cap_w: Optional[int] = None
    error: Optional[str] = None

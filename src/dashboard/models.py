"""Shared Pydantic models for the dashboard server.

These models are the single source of truth for data flowing through the
system: from hardware → sampling loop → regulators → WebSocket clients.

DeviceMode    – operating modes of the battery inverter
GridSample    – one measurement snapshot (Shelly + Zendure)
RegulatorInfo – metadata about a registered regulator
OscState      – oscillation detector state per phase
ControlStatus – what the active regulator computed last cycle
DashboardState – full snapshot broadcast via WebSocket every second
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

# ── Device modes ─────────────────────────────────────────────────────────────


class DeviceMode(str, Enum):
    """Operating modes of the battery inverter."""

    AC_CHARGE = "ac_charge"
    """Charge battery from grid at a fixed power."""

    IDLE = "idle"
    """Battery passive – no charge, no discharge."""

    DISCHARGE_ZERO_FEED = "discharge_zero_feed"
    """Discharge via the active regulator (zero-feed or custom)."""


# ── Hardware snapshot ─────────────────────────────────────────────────────────


class GridSample(BaseModel):
    """One measurement snapshot from Shelly + Zendure.

    All power values in Watt.  Positive = consumption / discharge.
    Negative = feed-in / charge.
    """

    timestamp: float = Field(description="Unix timestamp (seconds).")
    phase_a_w: float = Field(description="Grid power on phase A [W].")
    phase_b_w: float = Field(description="Grid power on phase B [W].")
    phase_c_w: float = Field(description="Grid power on phase C [W].")
    battery_output_w: float = Field(description="Battery AC output [W].")
    soc_percent: Optional[int] = Field(default=None, description="Battery SoC [%].")
    charge_input_w: Optional[float] = Field(
        default=None, description="Battery AC charge input [W]."
    )

    @property
    def total_grid_w(self) -> float:
        return self.phase_a_w + self.phase_b_w + self.phase_c_w

    @property
    def real_consumption_w(self) -> float:
        """Actual household consumption = grid + battery output."""
        return self.total_grid_w + self.battery_output_w


# ── Oscillation state ─────────────────────────────────────────────────────────


class OscState(BaseModel):
    """Oscillation detector state for one phase (or total)."""

    oscillating: bool = False
    limit_w: Optional[float] = None


# ── Controller output ─────────────────────────────────────────────────────────


class ControlStatus(BaseModel):
    """What the active regulator computed in its last control cycle."""

    regulator_name: str
    setpoint_w: int = Field(description="Final setpoint sent to battery [W].")
    setpoint_changed: bool = False
    raw_target_w: Optional[float] = None
    ff_output_w: Optional[float] = None
    feedback_output_w: Optional[float] = None
    osc_limit_w: Optional[float] = None
    osc_a: OscState = Field(default_factory=OscState)
    osc_b: OscState = Field(default_factory=OscState)
    osc_c: OscState = Field(default_factory=OscState)
    osc_total: OscState = Field(default_factory=OscState)


# ── Regulator metadata ────────────────────────────────────────────────────────


class RegulatorInfo(BaseModel):
    """Metadata about a registered regulator returned by the API."""

    name: str
    description: str = ""
    is_active: bool = False
    settings_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema for the regulator settings (for GUI form generation).",
    )
    current_settings: dict[str, Any] = Field(
        default_factory=dict,
        description="Current settings values.",
    )


# ── Full dashboard state ──────────────────────────────────────────────────────


class DashboardState(BaseModel):
    """Full snapshot broadcast to all WebSocket clients every second."""

    timestamp: float
    mode: DeviceMode
    charge_power_w: Optional[int] = Field(
        default=None, description="Charge power in AC_CHARGE mode [W]."
    )
    max_discharge_w: int = Field(default=800, description="Max discharge power limit [W].")
    active_regulator: Optional[str] = None
    sample: Optional[GridSample] = None
    control: Optional[ControlStatus] = None
    error: Optional[str] = None


# ── WebSocket command models ──────────────────────────────────────────────────


class SetModeCommand(BaseModel):
    """Command to change the device operating mode."""

    mode: DeviceMode
    charge_power_w: Optional[int] = Field(
        default=None,
        ge=1,
        le=3000,
        description="Required when mode=AC_CHARGE.",
    )
    max_discharge_w: Optional[int] = Field(
        default=None,
        ge=1,
        le=3000,
        description="Optional discharge cap when mode=DISCHARGE_ZERO_FEED.",
    )


class SelectRegulatorCommand(BaseModel):
    """Command to switch the active regulator."""

    name: str


class UpdateSettingsCommand(BaseModel):
    """Command to update settings of a (possibly non-active) regulator."""

    name: str
    settings: dict[str, Any]

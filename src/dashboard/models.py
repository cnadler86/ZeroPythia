"""Dashboard command models and runtime model re-exports.

The dashboard package owns API command payload models.
Runtime/domain state models are defined in ``src.runtime.models`` and re-exported
here for a smoother migration.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from src.runtime.models import (  # re-exported runtime/domain models
    AutoStatus,
    ControlStatus,
    DashboardState,
    DeviceMode,
    GridSample,
    OscState,
    PlanSummaryEntry,
    RegulatorInfo,
)


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


class AutoConnectCommand(BaseModel):
    """Command to configure and start the GridPythia MQTT connection."""

    mqtt_broker: str = Field(description="MQTT broker URL, e.g. mqtt://192.168.1.10:1883.")
    device_id: str = Field(description="Inverter device ID as configured in GridPythia.")
    topic_prefix: str = Field(default="gridpythia", description="MQTT topic prefix.")
    status_interval_s: float = Field(
        default=60.0, ge=10.0, description="Status report interval [s]."
    )


__all__ = [
    "AutoConnectCommand",
    "AutoStatus",
    "ControlStatus",
    "DashboardState",
    "DeviceMode",
    "GridSample",
    "OscState",
    "PlanSummaryEntry",
    "RegulatorInfo",
    "SelectRegulatorCommand",
    "SetModeCommand",
    "UpdateSettingsCommand",
]

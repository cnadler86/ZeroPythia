"""Runtime domain package."""

from typing import TYPE_CHECKING

from .models import (
    AutoStatus,
    ControlStatus,
    DashboardState,
    DeviceMode,
    GridSample,
    OscState,
    PlanSummaryEntry,
    RegulatorInfo,
)

if TYPE_CHECKING:
    from .control_runtime import ControlRuntime


def __getattr__(name: str):
    if name == "ControlRuntime":
        from .control_runtime import ControlRuntime

        return ControlRuntime
    raise AttributeError(name)


__all__ = [
    "AutoStatus",
    "ControlStatus",
    "DashboardState",
    "DeviceMode",
    "GridSample",
    "OscState",
    "PlanSummaryEntry",
    "RegulatorInfo",
    "ControlRuntime",
]

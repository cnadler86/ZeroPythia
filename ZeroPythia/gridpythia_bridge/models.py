"""Pydantic models for the GridPythia ↔ ZeroFeedController MQTT integration.

These models mirror the relevant parts of GridPythia's API schema so the
controller can parse incoming plan messages without depending on the
GridPythia package.

MQTT plan topic:  ``gridpythia/inverters/{device_id}/plan``
MQTT status topic: ``gridpythia/inverters/{device_id}/status``
"""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum
from typing import Optional

from pydantic import BaseModel, Field


class InverterMode(IntEnum):
    """GridPythia inverter operating modes.

    Must stay in sync with ``GridPythia.optimization.solution.InverterMode``.
    """

    IDLE = 0
    """Battery passive; PV flows directly to load."""

    DISCHARGE = 1
    """Battery discharges; excess may feed into the grid."""

    DISCHARGE_ZERO_FEED_IN = 2
    """Battery discharges; no export to grid (zero feed-in control active)."""

    AC_CHARGE = 3
    """Grid charges the battery; excess PV may export."""

    AC_CHARGE_ZERO_FEED_IN = 4
    """Grid charges the battery; no grid export."""


class PlanStep(BaseModel):
    """One time slot from the GridPythia optimizer schedule."""

    timestamp: datetime = Field(description="Wall-clock start of this slot (ISO 8601 with tz).")
    mode: InverterMode = Field(description="Operating mode for this slot.")
    mode_name: str = Field(default="", description="Human-readable mode name.")

    # Energy budgets for this slot [Wh] — divide by dt_hours to get average W
    discharge_ac_wh: float = Field(default=0.0, description="Planned AC discharge energy [Wh].")
    charge_ac_wh: float = Field(default=0.0, description="Planned AC charge energy [Wh].")
    pv_to_ac_wh: float = Field(default=0.0, description="PV energy to AC bus [Wh].")
    pv_to_battery_wh: float = Field(default=0.0, description="PV energy stored in battery [Wh].")
    battery_soc_wh: Optional[float] = Field(
        default=None, description="Battery SoC at end of slot [Wh]."
    )

    def max_discharge_w(self, dt_hours: float = 0.25) -> float:
        """Average discharge power for this slot [W]."""
        if dt_hours <= 0:
            return 0.0
        return self.discharge_ac_wh / dt_hours

    def max_charge_w(self, dt_hours: float = 0.25) -> float:
        """Average charge power for this slot [W]."""
        if dt_hours <= 0:
            return 0.0
        return self.charge_ac_wh / dt_hours


class InverterPlan(BaseModel):
    """Full plan published by GridPythia after each optimization run."""

    device_id: str = Field(description="Inverter device ID (matches config).")
    published_at: datetime = Field(description="UTC timestamp when the plan was published.")
    dt_hours: float = Field(
        default=0.25,
        description="Duration of each time slot in hours (e.g. 0.25 = 15 min).",
    )
    steps: list[PlanStep] = Field(default_factory=list, description="Ordered list of plan steps.")

    def get_current_step(self, now: Optional[datetime] = None) -> Optional[PlanStep]:
        """Return the plan step that covers *now* (or the current time).

        Returns ``None`` when the plan has no steps or all steps are in the past.
        """
        from datetime import timezone

        if now is None:
            now = datetime.now(tz=timezone.utc)

        # Normalise to UTC for comparison
        now_utc = now.astimezone(timezone.utc)

        for step in self.steps:
            step_start = step.timestamp.astimezone(timezone.utc)
            step_end_s = step_start.timestamp() + self.dt_hours * 3600
            if step_start.timestamp() <= now_utc.timestamp() < step_end_s:
                return step

        # No active step — return the last step if it ended less than one slot ago
        # (guards against minor clock drift / publish delays).
        if self.steps:
            last = self.steps[-1]
            last_end = last.timestamp.astimezone(timezone.utc).timestamp() + self.dt_hours * 3600
            if now_utc.timestamp() < last_end + self.dt_hours * 3600:
                return last

        return None

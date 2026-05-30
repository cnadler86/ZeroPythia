"""Unit tests for PlanStep and InverterPlan models.

Covers:
- PlanStep.max_discharge_w / max_charge_w (fixed from @property to methods)
- InverterPlan.get_current_step
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ZeroPythia.gridpythia_bridge.models import InverterMode, InverterPlan, PlanStep


class TestPlanStepPower:
    """Verify max_discharge_w and max_charge_w are proper methods (not properties)."""

    def test_max_discharge_w_default_dt(self) -> None:
        step = PlanStep(
            timestamp=datetime.now(tz=timezone.utc),
            mode=InverterMode.DISCHARGE,
            discharge_ac_wh=200.0,
        )
        # 200 Wh / 0.25 h = 800 W
        assert step.max_discharge_w() == pytest.approx(800.0)

    def test_max_discharge_w_custom_dt(self) -> None:
        step = PlanStep(
            timestamp=datetime.now(tz=timezone.utc),
            mode=InverterMode.DISCHARGE,
            discharge_ac_wh=200.0,
        )
        # 200 Wh / 0.5 h = 400 W
        assert step.max_discharge_w(dt_hours=0.5) == pytest.approx(400.0)

    def test_max_discharge_w_zero_dt_returns_zero(self) -> None:
        step = PlanStep(
            timestamp=datetime.now(tz=timezone.utc),
            mode=InverterMode.DISCHARGE,
            discharge_ac_wh=200.0,
        )
        assert step.max_discharge_w(dt_hours=0.0) == 0.0

    def test_max_charge_w_default_dt(self) -> None:
        step = PlanStep(
            timestamp=datetime.now(tz=timezone.utc),
            mode=InverterMode.AC_CHARGE,
            charge_ac_wh=100.0,
        )
        # 100 Wh / 0.25 h = 400 W
        assert step.max_charge_w() == pytest.approx(400.0)

    def test_max_charge_w_custom_dt(self) -> None:
        step = PlanStep(
            timestamp=datetime.now(tz=timezone.utc),
            mode=InverterMode.AC_CHARGE,
            charge_ac_wh=100.0,
        )
        # 100 Wh / 1.0 h = 100 W
        assert step.max_charge_w(dt_hours=1.0) == pytest.approx(100.0)

    def test_max_charge_w_negative_dt_returns_zero(self) -> None:
        step = PlanStep(
            timestamp=datetime.now(tz=timezone.utc),
            mode=InverterMode.AC_CHARGE,
            charge_ac_wh=100.0,
        )
        assert step.max_charge_w(dt_hours=-1.0) == 0.0


class TestInverterPlanGetCurrentStep:

    def _make_plan(self, *, dt_hours: float = 0.25) -> InverterPlan:
        now = datetime.now(tz=timezone.utc)
        slot_s = dt_hours * 3600
        steps = []
        for i in range(4):
            ts = now - timedelta(seconds=slot_s) + timedelta(seconds=slot_s * i)
            steps.append(
                PlanStep(
                    timestamp=ts,
                    mode=InverterMode.DISCHARGE_ZERO_FEED_IN,
                    discharge_ac_wh=200.0,
                )
            )
        return InverterPlan(
            device_id="test",
            published_at=now,
            dt_hours=dt_hours,
            steps=steps,
        )

    def test_returns_current_step(self) -> None:
        plan = self._make_plan()
        step = plan.get_current_step()
        assert step is not None
        assert step.mode == InverterMode.DISCHARGE_ZERO_FEED_IN

    def test_returns_none_for_empty_plan(self) -> None:
        plan = InverterPlan(
            device_id="test",
            published_at=datetime.now(tz=timezone.utc),
            steps=[],
        )
        assert plan.get_current_step() is None

    def test_returns_none_for_fully_past_plan(self) -> None:
        """Plan that ended more than 2 slots ago should return None."""
        now = datetime.now(tz=timezone.utc)
        old_ts = now - timedelta(hours=24)
        plan = InverterPlan(
            device_id="test",
            published_at=old_ts,
            dt_hours=0.25,
            steps=[
                PlanStep(
                    timestamp=old_ts,
                    mode=InverterMode.IDLE,
                )
            ],
        )
        assert plan.get_current_step() is None

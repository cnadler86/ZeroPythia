"""Plan executor: translates GridPythia inverter plan steps into device commands.

The executor is the glue between the GridPythia optimisation plan and the
actual hardware control.  It runs as an asyncio task alongside the
:class:`~src.controller.zerofeed_v3.ZeroFeedV3Controller` and determines,
for each 15-minute optimization slot, which operating mode to activate.

Mode dispatch logic
-------------------
+-----------------------------+-----------------------------------------+
| GridPythia mode             | Action                                  |
+=============================+=========================================+
| IDLE (0)                    | ``battery.stop()``                      |
+-----------------------------+-----------------------------------------+
| DISCHARGE (1)               | ``battery.start_discharge(plan_w)``     |
|                             | (fixed power, no zero-feed loop)        |
+-----------------------------+-----------------------------------------+
| DISCHARGE_ZERO_FEED_IN (2)  | ZeroFeedV3Controller runs normally,     |
|                             | max output capped to planned power.     |
+-----------------------------+-----------------------------------------+
| AC_CHARGE (3)               | Pause zero-feed loop,                   |
| AC_CHARGE_ZERO_FEED_IN (4)  | ``battery.start_charge(plan_charge_w)`` |
+-----------------------------+-----------------------------------------+
| No plan / stale plan        | DISCHARGE_ZERO_FEED_IN fallback         |
+-----------------------------+-----------------------------------------+

The executor checks the plan roughly once per optimization slot
(``check_interval_s``, default 60 s) and reacts to mode changes.  The
:class:`~src.controller.zerofeed_v3.ZeroFeedV3Controller` keeps running its
fast ~3 s control loop in the background — the executor only adjusts its
``settings.manager.max_output_w`` to honour the plan's energy budget.

Usage::

    executor = PlanExecutor(
        battery=solarflow,
        zero_feed_ctrl=controller,
        plan_subscriber=subscriber,
        device_id="SF800Pro",
        config_max_output_w=800,
        config_min_output_w=20,
    )
    task = asyncio.create_task(executor.run())
    ...
    task.cancel()
    await task
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from src.controller.zerofeed_v3 import BatteryInverter, ZeroFeedV3Controller
from src.gridpythia.models import InverterMode, PlanStep
from src.gridpythia.plan_subscriber import GridPythiaPlanSubscriber

logger = logging.getLogger(__name__)


class PlanExecutor:
    """Asyncio task that applies the GridPythia plan to the battery.

    Parameters
    ----------
    battery:
        The Zendure (or mock) battery client — must implement the full
        :class:`~src.controller.zerofeed_v3.BatteryInverter` protocol,
        including ``start_charge()``.
    zero_feed_ctrl:
        The running :class:`~src.controller.zerofeed_v3.ZeroFeedV3Controller`
        instance.  Its ``settings.manager.max_output_w`` is adjusted on every
        plan step change to honour the energy budget.
    plan_subscriber:
        Provides the current plan step via ``get_current_step()``.
    device_id:
        Inverter device ID (for logging only).
    config_max_output_w:
        Maximum discharge power from the static config — used as the fallback
        cap when no plan is active.
    config_min_output_w:
        Minimum discharge power from the static config.
    check_interval_s:
        How often the executor checks for a plan step change (seconds).
        Should be ≤ ``dt_hours * 3600`` to catch transitions in time.
    """

    def __init__(
        self,
        battery: BatteryInverter,
        zero_feed_ctrl: ZeroFeedV3Controller,
        plan_subscriber: GridPythiaPlanSubscriber,
        device_id: str,
        config_max_output_w: int,
        config_min_output_w: int,
        check_interval_s: float = 60.0,
    ) -> None:
        self._battery = battery
        self._ctrl = zero_feed_ctrl
        self._subscriber = plan_subscriber
        self._device_id = device_id
        self._config_max_w = config_max_output_w
        self._config_min_w = config_min_output_w
        self._interval = check_interval_s

        # Track which mode is currently active so we only act on changes.
        self._active_mode: Optional[InverterMode] = None
        # True while the zero-feed loop should be paused (charging or idle).
        self._zf_paused: bool = False

    async def run(self) -> None:
        """Run the plan executor loop until cancelled."""
        logger.info("plan_executor_started", extra={"device_id": self._device_id})
        try:
            while True:
                await self._tick()
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("plan_executor_stopped", extra={"device_id": self._device_id})

    # ── Internal ─────────────────────────────────────────────────────────

    async def _tick(self) -> None:
        """Check the current plan step and act if mode changed."""
        now = datetime.now(tz=timezone.utc)
        step = self._subscriber.get_current_step(now)

        if step is None:
            await self._apply_fallback()
        else:
            await self._apply_step(step)

    async def _apply_fallback(self) -> None:
        """No active plan — fall back to zero-feed discharge."""
        if self._active_mode == InverterMode.DISCHARGE_ZERO_FEED_IN and not self._zf_paused:
            # Already in the right mode with correct cap — nothing to do.
            return

        logger.info(
            "plan_fallback",
            extra={
                "device_id": self._device_id,
                "reason": "no active plan",
                "max_w": self._config_max_w,
            },
        )
        self._ctrl.settings.manager.max_output_w = self._config_max_w
        await self._ensure_zero_feed_running()
        self._active_mode = InverterMode.DISCHARGE_ZERO_FEED_IN

    async def _apply_step(self, step: PlanStep) -> None:
        """Apply the given plan step."""
        mode = step.mode
        dt_hours = getattr(self._subscriber._plan, "dt_hours", 0.25)

        if mode == InverterMode.IDLE:
            if self._active_mode == InverterMode.IDLE:
                return
            logger.info(
                "plan_mode_change",
                extra={"device_id": self._device_id, "mode": "IDLE"},
            )
            await self._ensure_zero_feed_stopped()
            await self._battery.stop()
            self._active_mode = InverterMode.IDLE

        elif mode == InverterMode.DISCHARGE:
            # Fixed power discharge — zero-feed loop is paused.
            plan_w = int(step.discharge_ac_wh / dt_hours) if dt_hours > 0 else self._config_max_w
            plan_w = max(self._config_min_w, min(self._config_max_w, plan_w))

            if self._active_mode == InverterMode.DISCHARGE and not self._zf_paused:
                # Mode unchanged; update power if needed via the ZeroFeed cap.
                # Use the same path as ZFI but without the reactive loop.
                return
            logger.info(
                "plan_mode_change",
                extra={"device_id": self._device_id, "mode": "DISCHARGE", "power_w": plan_w},
            )
            # Pause zero-feed loop, then set a fixed output.
            await self._ensure_zero_feed_stopped()
            await self._battery.start_discharge(plan_w)
            self._active_mode = InverterMode.DISCHARGE

        elif mode == InverterMode.DISCHARGE_ZERO_FEED_IN:
            plan_max_w = (
                int(step.discharge_ac_wh / dt_hours) if dt_hours > 0 else self._config_max_w
            )
            plan_max_w = max(self._config_min_w, min(self._config_max_w, plan_max_w))

            # Update the cap so the zero-feed controller respects the energy budget.
            self._ctrl.settings.manager.max_output_w = plan_max_w

            if self._active_mode == InverterMode.DISCHARGE_ZERO_FEED_IN and not self._zf_paused:
                # Already running — cap update above is enough.
                return
            logger.info(
                "plan_mode_change",
                extra={
                    "device_id": self._device_id,
                    "mode": "DISCHARGE_ZERO_FEED_IN",
                    "max_w": plan_max_w,
                },
            )
            await self._ensure_zero_feed_running()
            self._active_mode = InverterMode.DISCHARGE_ZERO_FEED_IN

        elif mode in (InverterMode.AC_CHARGE, InverterMode.AC_CHARGE_ZERO_FEED_IN):
            plan_charge_w = int(step.charge_ac_wh / dt_hours) if dt_hours > 0 else 0
            plan_charge_w = max(0, plan_charge_w)

            if self._active_mode == mode and self._zf_paused:
                # Mode unchanged — nothing to do.
                return
            logger.info(
                "plan_mode_change",
                extra={
                    "device_id": self._device_id,
                    "mode": mode.name,
                    "charge_w": plan_charge_w,
                },
            )
            await self._ensure_zero_feed_stopped()
            if plan_charge_w > 0:
                await self._battery.start_charge(plan_charge_w)
            else:
                # 0 Wh charge budget this slot → just stop
                await self._battery.stop()
            self._active_mode = mode

    # ── Zero-feed loop management ─────────────────────────────────────────

    async def _ensure_zero_feed_running(self) -> None:
        """Restart the zero-feed control loop if it was paused."""
        if not self._zf_paused:
            return
        logger.debug("plan_executor_resuming_zf", extra={"device_id": self._device_id})
        await self._ctrl.start()
        self._zf_paused = False

    async def _ensure_zero_feed_stopped(self) -> None:
        """Stop the zero-feed control loop if it is running."""
        if self._zf_paused:
            return
        if self._ctrl._running:
            logger.debug("plan_executor_pausing_zf", extra={"device_id": self._device_id})
            # Stop the controller loops but leave battery command to the caller.
            self._ctrl._running = False
            tasks = [
                t
                for t in (self._ctrl._sampling_task, self._ctrl._control_task)
                if t is not None and not t.done()
            ]
            if tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True), timeout=5.0
                    )
                except asyncio.TimeoutError:
                    for t in tasks:
                        t.cancel()
        self._zf_paused = True

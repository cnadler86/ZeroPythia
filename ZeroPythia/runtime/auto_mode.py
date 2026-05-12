"""Auto mode manager – GridPythia MQTT plan subscriber + dispatcher.

When AUTO mode is active the ``AutoModeManager`` periodically checks the current
GridPythia plan step (via ``GridPythiaPlanSubscriber``) and calls
``ControlRuntime.apply_effective_mode()`` to dispatch the appropriate device
command.

Plan summary
------------
Consecutive plan slots that share the same mode *and* a similar power level are
merged into a single ``PlanSummaryEntry`` for the dashboard (up to
``MAX_SUMMARY_ENTRIES``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine, Optional

from clients.mqtt.client import MqttClient, MqttConfig
from ZeroPythia.gridpythia_bridge.models import InverterMode, InverterPlan, PlanStep
from ZeroPythia.gridpythia_bridge.plan_subscriber import GridPythiaPlanSubscriber
from ZeroPythia.gridpythia_bridge.status_reporter import GridPythiaStatusReporter
from ZeroPythia.runtime.models import AutoStatus, DeviceMode, PlanSummaryEntry

logger = logging.getLogger(__name__)

# Async callback: (mode, charge_power_w, max_discharge_w) → None
ApplyModeCb = Callable[
    [DeviceMode, Optional[int], Optional[int]],
    Coroutine[Any, Any, None],
]

_MODE_LABELS: dict[InverterMode, str] = {
    InverterMode.IDLE: "Idle",
    InverterMode.DISCHARGE: "Discharge",
    InverterMode.DISCHARGE_ZERO_FEED_IN: "Zero-Feed",
    InverterMode.AC_CHARGE: "AC Charge",
    InverterMode.AC_CHARGE_ZERO_FEED_IN: "AC Charge",
}

MAX_SUMMARY_ENTRIES = 6


# ── Plan helper functions ──────────────────────────────────────────────────────


def _plan_power_w(step: PlanStep, dt_hours: float) -> Optional[int]:
    """Return the relevant power value for a plan step in Watts."""
    if dt_hours <= 0:
        return None
    # For ZFI/TFI-style discharge modes, GridPythia discharge Wh is not used as
    # an effective runtime cap in dashboard control. Showing a number here would
    # be misleading, so keep the summary power empty for those slots.
    if step.mode in (InverterMode.DISCHARGE, InverterMode.DISCHARGE_ZERO_FEED_IN):
        return None
    if step.mode in (InverterMode.AC_CHARGE, InverterMode.AC_CHARGE_ZERO_FEED_IN):
        return int(step.charge_ac_wh / dt_hours)
    return None


def _same_action(a: PlanStep, b: PlanStep, dt_hours: float) -> bool:
    """True when two steps can be merged into one summary entry."""
    if a.mode != b.mode:
        return False
    pw_a = _plan_power_w(a, dt_hours)
    pw_b = _plan_power_w(b, dt_hours)
    if pw_a is None and pw_b is None:
        return True
    if pw_a is None or pw_b is None:
        return False
    # Allow 5% tolerance or 10 W, whichever is larger
    return abs(pw_a - pw_b) <= max(10, int(0.05 * pw_a))


def _make_entry(
    step: PlanStep,
    start_dt: datetime,
    end_ts: float,
    plan: InverterPlan,
) -> PlanSummaryEntry:
    """Build one PlanSummaryEntry from a merged group."""
    # Convert to local time for display
    start_local = start_dt.astimezone()
    end_local = datetime.fromtimestamp(end_ts, tz=start_local.tzinfo)

    today_date = datetime.now().date()
    tomorrow_date = today_date + timedelta(days=1)
    entry_date = start_local.date()

    if entry_date == today_date:
        date_label = None
    elif entry_date == tomorrow_date:
        date_label = "Tomorrow"
    else:
        date_label = start_local.strftime("%a")  # e.g. "Mon", "Tue"

    end_next_day = end_local.date() != start_local.date()

    return PlanSummaryEntry(
        mode_label=_MODE_LABELS.get(step.mode, step.mode.name),
        from_time=start_local.strftime("%H:%M"),
        to_time=end_local.strftime("%H:%M"),
        power_w=_plan_power_w(step, plan.dt_hours),
        date=date_label,
        end_next_day=end_next_day,
    )


def build_plan_summary(plan: InverterPlan, now: datetime) -> list[PlanSummaryEntry]:
    """Return merged plan summary starting from *now* (at most MAX_SUMMARY_ENTRIES)."""
    now_ts = now.timestamp()
    dt_s = plan.dt_hours * 3600

    # Keep only slots that are not fully in the past
    future = [
        s for s in plan.steps if s.timestamp.astimezone(timezone.utc).timestamp() + dt_s > now_ts
    ]
    if not future:
        return []

    entries: list[PlanSummaryEntry] = []
    g_start = future[0].timestamp
    g_step = future[0]
    g_end_ts = g_start.astimezone(timezone.utc).timestamp() + dt_s

    for step in future[1:]:
        step_ts = step.timestamp.astimezone(timezone.utc).timestamp()
        if _same_action(step, g_step, plan.dt_hours):
            g_end_ts = step_ts + dt_s  # extend group
        else:
            entries.append(_make_entry(g_step, g_start, g_end_ts, plan))
            if len(entries) >= MAX_SUMMARY_ENTRIES:
                return entries
            g_start = step.timestamp
            g_step = step
            g_end_ts = step_ts + dt_s

    entries.append(_make_entry(g_step, g_start, g_end_ts, plan))
    return entries[:MAX_SUMMARY_ENTRIES]


# ── AutoModeManager ────────────────────────────────────────────────────────────


class AutoModeManager:
    """Drives the ControlRuntime based on a live GridPythia plan.

    The manager holds an MQTT client, a ``GridPythiaPlanSubscriber``, and a
    ``GridPythiaStatusReporter``.  On each ``tick()`` it reads the current plan
    step and calls the supplied *apply_cb* when the effective mode should change.

    Parameters
    ----------
    mqtt_broker:
        Broker URL, e.g. ``mqtt://192.168.1.10:1883``.
    device_id:
        Inverter device ID matching GridPythia config.
    battery:
        Zendure SolarFlowBase-compatible client (for SoC reading in status reports).
    config_max_w:
        Maximum discharge power cap [W] – used when the plan yields a higher value.
    config_min_w:
        Minimum allowed battery output [W].
    topic_prefix:
        MQTT topic prefix (must match GridPythia server config).
    status_interval_s:
        How often SoC + mode are reported to GridPythia [s].
    """

    def __init__(
        self,
        mqtt_broker: str,
        device_id: str,
        battery: Any,
        config_max_w: int = 800,
        config_min_w: int = 20,
        topic_prefix: str = "gridpythia",
        status_interval_s: float = 60.0,
    ) -> None:
        self._device_id = device_id
        self._config_max_w = config_max_w
        self._config_min_w = config_min_w

        mqtt_cfg = MqttConfig(
            broker=mqtt_broker,
            client_id=f"dashboard-{device_id}",
            topic_prefix=topic_prefix,
        )
        self._mqtt_client = MqttClient(mqtt_cfg)

        self._subscriber = GridPythiaPlanSubscriber(
            mqtt_client=self._mqtt_client,
            device_id=device_id,
            topic_prefix=topic_prefix,
        )
        self._subscriber.register()

        self._reporter = GridPythiaStatusReporter(
            mqtt_client=self._mqtt_client,
            battery=battery,
            device_id=device_id,
            topic_prefix=topic_prefix,
            interval_s=status_interval_s,
        )

        # Internal state
        self._connected = False
        self._last_inv_mode: Optional[InverterMode] = None
        self._last_charge_power_w: Optional[int] = (
            None  # track AC charge power for change detection
        )
        self._in_fallback: bool = False  # True when last dispatch was the no-plan fallback
        self._effective_mode_label: str = "–"
        self._plan_summary: list[PlanSummaryEntry] = []
        self._reporter_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the MQTT connection (call before the asyncio event loop runs tick)."""
        self._mqtt_client.start()
        self._connected = True
        logger.info(
            "AutoModeManager started",
            extra={"device_id": self._device_id},
        )

    def stop(self) -> None:
        """Stop MQTT and cancel the status reporter task."""
        self._mqtt_client.stop()
        self._connected = False
        logger.info("AutoModeManager stopped", extra={"device_id": self._device_id})

    async def start_reporter_task(self) -> None:
        """Start the status reporter as an asyncio task (call after event loop is running)."""
        if self._reporter_task is not None:
            return
        self._reporter_task = asyncio.create_task(self._reporter.run(), name="auto-status-reporter")

    async def stop_reporter_task(self) -> None:
        """Cancel the status reporter task gracefully."""
        if self._reporter_task is None:
            return
        self._reporter_task.cancel()
        try:
            await self._reporter_task
        except asyncio.CancelledError:
            pass
        finally:
            self._reporter_task = None

    # ── Tick ──────────────────────────────────────────────────────────────────

    async def tick(self, apply_cb: ApplyModeCb) -> None:
        """Check the current plan step and dispatch via *apply_cb* if the mode changed.

        *apply_cb* is an async callable: ``(mode, charge_power_w, max_discharge_w) → None``
        matching :py:meth:`ControlRuntime.apply_effective_mode`.

        Fallback behavior:
        - No plan received yet: fall back to safe default.
        - Plan exists but no step is time-active: keep current status unchanged
          (wait for first scheduled step to become active).
        """
        now = datetime.now(tz=timezone.utc)
        self._refresh_plan_summary(now)

        step = self._subscriber.get_current_step(now)
        if step is None:
            # Distinguish: has a plan been received?
            if self._subscriber.has_plan:
                # Plan exists but no step is currently time-active.
                # Keep the current status – don't override it until the plan activates.
                return
            else:
                # No plan has ever been received → fall back to safe default.
                await self._apply_fallback(apply_cb)
        else:
            await self._apply_step(step, apply_cb)

    # ── Status for dashboard ──────────────────────────────────────────────────

    def get_auto_status(self) -> AutoStatus:
        """Return current auto mode status for inclusion in DashboardState."""
        plan = self._subscriber._plan  # noqa: SLF001 – read via property missing
        published_at: Optional[str] = None
        received_at: Optional[str] = None
        if plan is not None:
            # Plan timestamp provided by GridPythia payload (authoritative value).
            published_at = plan.published_at.astimezone().strftime("%d.%m.%Y %H:%M:%S")
            # Backward-compatible short field still used by older UI code.
            received_at = plan.published_at.astimezone().strftime("%H:%M")
            # Refresh summary inline so callers always see the current state,
            # even when called between two tick() invocations.
            self._refresh_plan_summary(datetime.now(tz=timezone.utc))
        return AutoStatus(
            connected=self._connected,
            has_plan=self._subscriber.has_plan,
            plan_published_at=published_at,
            plan_received_at=received_at,
            effective_mode=self._effective_mode_label,
            plan_summary=list(self._plan_summary),
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _apply_fallback(self, cb: ApplyModeCb) -> None:
        """No plan active → fall back to zero-feed discharge with config cap."""
        if self._last_inv_mode == InverterMode.DISCHARGE_ZERO_FEED_IN and self._in_fallback:
            return  # already in fallback, nothing changed
        logger.info(
            "AutoMode: no plan active, falling back to DISCHARGE_ZERO_FEED",
            extra={"device_id": self._device_id},
        )
        await cb(DeviceMode.DISCHARGE_ZERO_FEED, None, self._config_max_w)
        self._last_inv_mode = InverterMode.DISCHARGE_ZERO_FEED_IN
        self._last_charge_power_w = None
        self._in_fallback = True
        self._effective_mode_label = "Zero-Feed (Fallback)"

    async def _apply_step(self, step: PlanStep, cb: ApplyModeCb) -> None:
        """Dispatch a plan step – only calls *cb* when the mode or power actually changes."""
        mode = step.mode
        plan = self._subscriber._plan  # noqa: SLF001
        dt_hours = plan.dt_hours if plan is not None else 0.25

        # Pre-compute charge power for AC_CHARGE modes so we can detect power changes
        # even when the mode itself stays the same.
        charge_w: Optional[int] = None
        if mode in (InverterMode.AC_CHARGE, InverterMode.AC_CHARGE_ZERO_FEED_IN):
            charge_w = int(step.charge_ac_wh / dt_hours) if dt_hours > 0 else 400
            charge_w = max(100, min(3000, charge_w))

        # Skip if nothing changed
        if mode == self._last_inv_mode and not self._in_fallback:
            if charge_w is None or charge_w == self._last_charge_power_w:
                return  # mode and power both unchanged
            # AC_CHARGE power changed → fall through to dispatch

        logger.info(
            "AutoMode plan step change: %s → %s",
            self._last_inv_mode,
            mode.name,
            extra={"device_id": self._device_id},
        )

        if mode == InverterMode.IDLE:
            await cb(DeviceMode.IDLE, None, None)

        elif mode in (InverterMode.DISCHARGE, InverterMode.DISCHARGE_ZERO_FEED_IN):
            # GridPythia signals the desired mode; for ZFI the discharge power is
            # not a fixed limit – use the hardware's own configured cap so the
            # zero-feed regulator runs without an artificial energy constraint.
            await cb(DeviceMode.DISCHARGE_ZERO_FEED, None, self._config_max_w)

        elif mode in (InverterMode.AC_CHARGE, InverterMode.AC_CHARGE_ZERO_FEED_IN):
            await cb(DeviceMode.AC_CHARGE, charge_w, None)

        self._last_inv_mode = mode
        self._last_charge_power_w = charge_w
        self._in_fallback = False
        self._effective_mode_label = _MODE_LABELS.get(mode, mode.name)

    def _refresh_plan_summary(self, now: datetime) -> None:
        """Rebuild the merged plan summary (cheap – call on every tick)."""
        plan = self._subscriber._plan  # noqa: SLF001
        if plan is None or not plan.steps:
            self._plan_summary = []
        else:
            self._plan_summary = build_plan_summary(plan, now)

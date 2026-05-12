"""Subscribes to GridPythia's inverter plan topic and exposes the current step.

GridPythia publishes the optimizer schedule as a retained MQTT message after
each successful optimization run:

    Topic:   gridpythia/inverters/{device_id}/plan
    Payload: {device_id, published_at, dt_hours, steps: [...]}

The subscriber stores the last received plan and provides
:meth:`get_current_step` to return the step that should be active right now.

If no plan has been received, or the plan is stale (all steps are in the past
and the last step ended more than *stale_after_s* seconds ago), the subscriber
reports ``None`` so the caller can fall back to a safe default (e.g. zero-feed
mode).

Usage::

    subscriber = GridPythiaPlanSubscriber(
        mqtt_client=client,
        device_id="SF800Pro",
        topic_prefix="gridpythia",
    )
    subscriber.register()    # registers MQTT callback, call before client.start()

    # later, from control loop:
    step = subscriber.get_current_step()
    if step is None:
        # no plan → fall back
    else:
        # execute step.mode
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

from pydantic import ValidationError

from clients.mqtt.client import MqttClient
from ZeroPythia.gridpythia.models import InverterPlan, PlanStep

logger = logging.getLogger(__name__)

# How long after the last plan step's end-time we still consider a plan valid.
# After this, get_current_step() returns None (forces fallback behaviour).
_DEFAULT_STALE_AFTER_S: float = 1800.0  # 30 minutes


class GridPythiaPlanSubscriber:
    """Thread-safe subscriber for GridPythia inverter plans.

    The MQTT callback runs in paho's network thread; all shared state is
    protected by a :class:`threading.Lock`.
    """

    def __init__(
        self,
        mqtt_client: MqttClient,
        device_id: str,
        topic_prefix: str = "gridpythia",
        stale_after_s: float = _DEFAULT_STALE_AFTER_S,
    ) -> None:
        self._mqtt = mqtt_client
        self._device_id = device_id
        self._topic = f"{topic_prefix}/inverters/{device_id}/plan"
        self._stale_after_s = stale_after_s
        self._plan: Optional[InverterPlan] = None
        self._lock = threading.Lock()

    def register(self) -> None:
        """Register the MQTT callback.  Must be called before ``mqtt_client.start()``."""
        self._mqtt.subscribe(self._topic, self._on_message)
        logger.info(
            "plan_subscriber_registered",
            extra={"topic": self._topic},
        )

    # ── Public API ────────────────────────────────────────────────────────

    def get_current_step(self, now: Optional[datetime] = None) -> Optional[PlanStep]:
        """Return the plan step that is active right now, or ``None``.

        Returns ``None`` when:
        - no plan has been received yet,
        - the plan is stale (all steps are more than *stale_after_s* old).
        """
        if now is None:
            now = datetime.now(tz=timezone.utc)

        with self._lock:
            plan = self._plan

        if plan is None:
            return None

        # Check staleness: last step's end time + grace period
        if plan.steps:
            last = plan.steps[-1]
            last_end_ts = last.timestamp.astimezone(timezone.utc).timestamp() + plan.dt_hours * 3600
            if now.timestamp() > last_end_ts + self._stale_after_s:
                logger.debug(
                    "plan_stale",
                    extra={
                        "device_id": self._device_id,
                        "last_end": last.timestamp.isoformat(),
                    },
                )
                return None

        return plan.get_current_step(now)

    @property
    def has_plan(self) -> bool:
        """True if at least one plan has been received."""
        with self._lock:
            return self._plan is not None

    # ── MQTT callback (paho network thread) ───────────────────────────────

    def _on_message(self, topic: str, payload: dict) -> None:
        try:
            plan = InverterPlan.model_validate(payload)
        except ValidationError as exc:
            logger.warning(
                "plan_subscriber_parse_error",
                extra={"topic": topic, "error": str(exc)},
            )
            return

        steps = len(plan.steps)
        logger.info(
            "plan_received",
            extra={
                "device_id": plan.device_id,
                "steps": steps,
                "published_at": plan.published_at.isoformat(),
            },
        )

        with self._lock:
            current = self._plan
            if current is not None and plan.published_at < current.published_at:
                logger.info(
                    "plan_ignored_older",
                    extra={
                        "device_id": self._device_id,
                        "incoming_published_at": plan.published_at.isoformat(),
                        "current_published_at": current.published_at.isoformat(),
                    },
                )
                return
            self._plan = plan

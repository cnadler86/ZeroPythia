"""Thread-based paho-mqtt client for the ZeroFeed controller.

Mirrors the pattern used in GridPythia's mqtt_gateway: paho's network I/O
runs in its own thread so there is no asyncio event-loop conflict on Windows
(ProactorEventLoop does not support paho's add_reader/add_writer calls).

Usage::

    cfg = MqttConfig(broker="mqtt://192.168.1.10:1883", client_id="zerofeed")
    client = MqttClient(cfg)
    client.start()

    client.publish("gridpythia/inverters/SF800Pro/status", {"soc": 63.5, "mode": 2})
    client.subscribe("gridpythia/inverters/SF800Pro/plan", my_callback)

    client.stop()

The ``subscribe`` callback receives ``(topic: str, payload: dict)`` and is
called from paho's network thread — keep it non-blocking.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from threading import Event
from typing import Callable
from urllib.parse import urlparse

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


@dataclass
class MqttConfig:
    """Connection settings for the MQTT broker."""

    broker: str = "mqtt://localhost:1883"
    client_id: str = "zerofeed"
    topic_prefix: str = "gridpythia"
    username: str = ""
    password: str = ""


# Callback type: receives (topic, payload_dict)
MessageCallback = Callable[[str, dict], None]


class MqttClient:
    """Thin wrapper around paho-mqtt running in its own thread.

    Thread-safety: ``publish`` serialises the payload to JSON and calls
    paho's thread-safe ``publish()``.  ``subscribe`` and ``unsubscribe``
    must be called before ``start()`` or from the on_connect callback
    (paho resubscribes on reconnect automatically).
    """

    def __init__(self, cfg: MqttConfig) -> None:
        self._cfg = cfg
        self._stop = Event()
        self._subscriptions: dict[str, MessageCallback] = {}

        parsed = urlparse(cfg.broker)
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or 1883

        self._client = mqtt.Client(
            client_id=cfg.client_id,
            clean_session=True,
            protocol=mqtt.MQTTv311,
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        )
        if cfg.username:
            self._client.username_pw_set(cfg.username, cfg.password or None)

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        # Automatic reconnect: wait 1 s first, max 30 s
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)

    # ── Public API ────────────────────────────────────────────────────────

    def subscribe(self, topic: str, callback: MessageCallback) -> None:
        """Register a callback for a topic.  Call before ``start()``."""
        self._subscriptions[topic] = callback

    def publish(
        self,
        topic: str,
        payload: dict,
        *,
        qos: int = 0,
        retain: bool = False,
    ) -> None:
        """Publish a dict as JSON.  Safe to call from any thread."""
        try:
            self._client.publish(topic, json.dumps(payload), qos=qos, retain=retain)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mqtt_publish_failed", extra={"topic": topic, "error": str(exc)})

    @property
    def is_connected(self) -> bool:
        return self._client.is_connected()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Connect asynchronously and start paho's network thread."""
        try:
            self._client.connect_async(self._host, self._port, keepalive=60)
            self._client.loop_start()
            logger.info(
                "mqtt_client_starting",
                extra={"host": self._host, "port": self._port},
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("mqtt_client_start_failed", extra={"error": str(exc)})

    def stop(self) -> None:
        """Disconnect and stop the network thread."""
        self._stop.set()
        self._client.disconnect()
        self._client.loop_stop()
        logger.info("mqtt_client_stopped")

    # ── paho callbacks (run in paho's network thread) ─────────────────────

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: object,
        flags: object,
        reason_code: object,
        properties: object,
    ) -> None:
        if reason_code == 0:
            logger.info(
                "mqtt_connected",
                extra={"broker": self._cfg.broker},
            )
            for topic in self._subscriptions:
                client.subscribe(topic, qos=0)
                logger.debug("mqtt_subscribed", extra={"topic": topic})
        else:
            logger.warning(
                "mqtt_connect_refused",
                extra={"reason": str(reason_code)},
            )

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: object,
        disconnect_flags: object,
        reason_code: object,
        properties: object,
    ) -> None:
        if reason_code != 0:
            logger.warning(
                "mqtt_disconnected_unexpected",
                extra={"reason": str(reason_code)},
            )
        else:
            logger.info("mqtt_disconnected_clean")

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: object,
        msg: mqtt.MQTTMessage,
    ) -> None:
        topic = msg.topic
        callback = self._subscriptions.get(topic)
        if callback is None:
            return

        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "mqtt_bad_payload",
                extra={"topic": topic, "error": str(exc)},
            )
            return

        try:
            callback(topic, payload)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "mqtt_callback_error",
                extra={"topic": topic, "error": str(exc)},
            )

"""Send the newest value of each controller-owned MQTT topic under backpressure."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from paho.mqtt.client import Client, MQTTMessageInfo


LOG = logging.getLogger(__name__)
MAX_PENDING = 16
ACK_WARNING_S = 30.0


@dataclass(frozen=True)
class Publication:
    payload: str
    qos: int
    retain: bool


class StateOutbox:
    """Called on the asyncio loop. Paho completes receipts on its network thread."""

    def __init__(self, client: Client, loop: asyncio.AbstractEventLoop):
        self.client = client
        self.loop = loop
        self.latest: dict[str, Publication] = {}
        self.dirty: dict[str, None] = {}
        self.inflight: dict[str, tuple[MQTTMessageInfo, float]] = {}
        self._timer: asyncio.Handle | None = None
        self._next_warning = 0.0
        self._closed = False

    def subscribe(self, topic: str, qos: int = 0):
        return self.client.subscribe(topic, qos)

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
        value = Publication(payload, qos, retain)
        if value != self.latest.get(topic):
            self.latest[topic] = value
            self.dirty[topic] = None
            self._schedule(0)

    def replay(self, prefix: str = "") -> None:
        """Republish current values even if the receiver lost an earlier snapshot."""
        self.dirty.update((topic, None) for topic in self.latest if topic.startswith(prefix))
        self._schedule(0)

    def reconnected(self) -> None:
        # Paho discards unwritten QoS 0 packets on socket replacement. Their
        # receipts can never complete. Paho itself retries outstanding QoS 1.
        for topic in list(self.inflight):
            if self.latest[topic].qos == 0:
                del self.inflight[topic]
        self.replay()

    def _schedule(self, delay: float) -> None:
        if not self._closed and self._timer is None and self.client.is_connected():
            self._timer = self.loop.call_later(delay, self._flush)

    def _warn(self, message: str, *args: object) -> None:
        now = self.loop.time()
        if now >= self._next_warning:
            LOG.warning(message, *args)
            self._next_warning = now + ACK_WARNING_S

    def _flush(self) -> None:
        self._timer = None
        if not self.client.is_connected():
            # Paho retains unacknowledged QoS 1 messages for reconnection.
            return
        now = self.loop.time()
        # Allow latest state (QoS 0) through while discovery awaits PUBACKs.
        outstanding: Counter[int] = Counter()
        for topic, (receipt, started) in list(self.inflight.items()):
            # Reconnect can invalidate an initially successful QoS 0 receipt
            # on Paho's thread before our connected callback gets to run.
            try:
                delivered = receipt.is_published()
            except (RuntimeError, ValueError):
                del self.inflight[topic]
                self.dirty[topic] = None
                self._warn("MQTT delivery failed (rc=%s); retrying the latest value", receipt.rc)
                continue
            if delivered:
                del self.inflight[topic]
                continue
            qos = self.latest[topic].qos
            outstanding[qos] += 1
            if now - started >= ACK_WARNING_S:
                self._warn("MQTT QoS %s delivery pending for %.1fs (%d in flight, %d topics waiting)",
                           qos, now - started, len(self.inflight), len(self.dirty))

        for topic in list(self.dirty):
            value = self.latest[topic]
            if topic in self.inflight or outstanding[value.qos] >= MAX_PENDING:
                continue
            receipt = self.client.publish(topic, value.payload, qos=value.qos, retain=value.retain)
            if receipt.rc != 0:
                self._warn("MQTT publication rejected (rc=%s, %d topics waiting)",
                           receipt.rc, len(self.dirty))
                # A full QoS 1 queue must never prevent current QoS 0 state.
                outstanding[value.qos] = MAX_PENDING
                continue
            outstanding[value.qos] += 1
            self.inflight[topic] = (receipt, now)
            del self.dirty[topic]
        if self.dirty or self.inflight:
            self._schedule(0.1)

    def close(self) -> None:
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

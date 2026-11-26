from types import SimpleNamespace

from graber.mqtt_outbox import MAX_PENDING, StateOutbox


class Clock:
    def __init__(self):
        self.now = 0
        self.timer = None

    def time(self):
        return self.now

    def call_later(self, delay, callback):
        self.timer = (self.now + delay, callback)
        return SimpleNamespace(cancel=lambda: setattr(self, "timer", None))

    def tick(self):
        self.now, callback = self.timer
        self.timer = None
        callback()


class Client:
    def __init__(self):
        self.online = True
        self.acked = False
        self.reject = False
        self.sends = []

    def is_connected(self):
        return self.online

    def publish(self, topic, payload, qos, retain):
        if self.reject:
            return SimpleNamespace(rc=15)
        self.sends.append((topic, payload, qos))
        return SimpleNamespace(rc=0, is_published=lambda: qos == 0 or self.acked)


def test_discovery_backpressure_does_not_block_current_state(caplog):
    client, clock = Client(), Clock()
    outbox = StateOutbox(client, clock)
    for i in range(2 * MAX_PENDING):
        outbox.publish(f"discovery/{i}", "config", qos=1, retain=True)
    clock.tick()
    assert len(client.sends) == MAX_PENDING
    clock.now = 45
    for i in range(2000):
        outbox.publish("blind/position", str(i), qos=0)
    # Discovery ACKs stay withheld, but telemetry still gets to the broker.
    clock.timer[1]()
    assert "delivery pending" in caplog.text
    assert client.sends[-1] == ("blind/position", "1999", 0)
    assert sum(qos == 1 for _, _, qos in client.sends) == MAX_PENDING
    outbox.publish("blind/position", "closed", qos=0)
    clock.tick()
    assert client.sends[-1] == ("blind/position", "closed", 0)

    client.acked = True
    clock.tick()
    assert sum(qos == 1 for _, _, qos in client.sends) == 2 * MAX_PENDING
    clock.tick()
    assert clock.timer is None


def test_rejected_state_retries_latest_and_offline_reconnect_resends():
    client, clock = Client(), Clock()
    outbox = StateOutbox(client, clock)
    client.reject = True
    outbox.publish("battery", "92", qos=0, retain=True)
    clock.tick()
    assert not client.sends
    outbox.publish("battery", "95", qos=0, retain=True)
    client.reject = False
    clock.tick()
    assert client.sends == [("battery", "95", 0)]

    client.online = False
    outbox.publish("battery", "96", qos=0, retain=True)
    clock.tick()
    assert clock.timer is None
    client.online = True
    outbox.reconnected()
    clock.tick()
    assert client.sends[-1] == ("battery", "96", 0)
    clock.tick()
    # Even an unchanged value is sent once for a restarted subscriber.
    outbox.replay("battery")
    clock.tick()
    assert client.sends[-2:] == [("battery", "96", 0)] * 2


def test_reconnect_invalidates_receipt_before_connected_callback(caplog):
    from paho.mqtt.client import MQTT_ERR_CONN_LOST, MQTTMessageInfo

    client, clock = Client(), Clock()
    outbox = StateOutbox(client, clock)
    lost = MQTTMessageInfo(1)
    send = client.publish

    def publish(topic, payload, qos, retain):
        success = send(topic, payload, qos, retain)
        return lost if len(client.sends) == 1 else success

    client.publish = publish
    outbox.publish("battery", "91", qos=0)
    clock.tick()
    # Model the Paho reconnect thread failing its outstanding QoS 0 packet
    # after publish() succeeded but before asyncio sees on_connect.
    lost.rc = MQTT_ERR_CONN_LOST
    outbox.publish("battery", "93", qos=0)
    outbox.publish("position", "50", qos=0)
    clock.tick()
    assert "retrying the latest value" in caplog.text
    assert client.sends == [("battery", "91", 0), ("battery", "93", 0), ("position", "50", 0)]
    clock.tick()
    assert clock.timer is None

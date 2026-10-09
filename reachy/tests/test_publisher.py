"""The robot as a publisher into Hashi (mobile-app/server), checked against the
same validation rules as mobile-app/protocol.ts isMessage()."""

import json
import re
import threading
import time
from datetime import datetime

from reachy_mini_intermediator.publisher import HashiPublisher, hashi_message


def is_hashi_message(m):
    """Port of protocol.ts isMessage()."""
    return (isinstance(m, dict) and isinstance(m.get("id"), str) and 0 < len(m["id"]) <= 128
            and m.get("userId") in ("person-1", "person-2")
            and isinstance(m.get("text"), str) and m["text"].strip() and len(m["text"]) <= 10_000
            and isinstance(m.get("language"), str) and re.match(r"^[a-z]{2,3}(-[a-z0-9]{2,8})*$", m["language"])
            and datetime.fromisoformat(m["sentAt"].replace("Z", "+00:00")).tzinfo is not None)


class FakeHashi:
    """Plays mobile-app/server/index.ts for one publisher connection at a time."""

    def __init__(self, drop_after=None):
        self.history = []
        self.drop_after = drop_after
        self.connections = 0

    def connect(self, url):
        self.connections += 1
        return FakeHashiSocket(self)


class FakeHashiSocket:
    def __init__(self, server):
        self.server = server
        self.inbox = [json.dumps({"type": "snapshot", "messages": list(server.history)})]
        self.closed = False
        self.cv = threading.Condition()

    def send(self, raw):
        if self.closed:
            raise ConnectionError("closed")
        event = json.loads(raw)
        m = event.get("message")
        with self.cv:
            if event.get("type") != "message" or not is_hashi_message(m):
                self.inbox.append(json.dumps({"type": "error", "error": "Invalid message. See WEBSOCKET.md."}))
            elif any(x["id"] == m["id"] for x in self.server.history):
                self.inbox.append(json.dumps({"type": "error", "error": "Message IDs must be unique."}))
            else:
                self.server.history.append(m)
                self.inbox.append(json.dumps({"type": "message", "message": m}))
                if self.server.drop_after and len(self.server.history) == self.server.drop_after:
                    self.server.drop_after = None
                    self.closed = True
            self.cv.notify_all()

    def __iter__(self):
        while True:
            with self.cv:
                while not self.inbox and not self.closed:
                    self.cv.wait(0.2)
                if self.closed and not self.inbox:
                    return
                item = self.inbox.pop(0)
            yield item

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()


def utt(uid, speaker, text, lang="ja"):
    return {"id": uid, "speaker": speaker, "text": text, "lang": lang, "start": 1791540600.25, "final": True}


def test_message_mapping_is_valid_for_the_app():
    m = hashi_message(utt("u3", "a", " こんにちは "), "person-1", "reachy-1", None)
    assert is_hashi_message(m)
    assert m == {"id": "reachy-1-u3", "userId": "person-1", "text": "こんにちは", "language": "ja",
                 "sentAt": "2026-10-09T10:10:00.250Z"}
    assert hashi_message(utt("u4", "a", "   "), "person-1", "p", None) is None
    assert hashi_message(utt("u5", "a", "hi", lang=None), "person-1", "p", "en")["language"] == "en"


def test_publishes_in_order_and_resends_after_a_drop():
    server = FakeHashi(drop_after=2)
    errors = []
    pub = HashiPublisher("ws://hashi:8080", {"a": "person-1", "b": "person-2"}, languages=lambda pid: None,
                         connect=server.connect, on_error=errors.append)
    pub.start()
    try:
        assert pub.publish(utt("u1", "a", "はじめまして"))
        assert pub.publish(utt("u2", "b", "Nice to meet you", "en"))
        assert not pub.publish(utt("u3", None, "who said this?"))  # no userId possible
        assert pub.publish(utt("u4", "a", "ラーメン行こう"))
        deadline = time.monotonic() + 8
        while len(server.history) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        pub.stop()
        pub.join(3)
    assert [(m["userId"], m["text"]) for m in server.history] == [
        ("person-1", "はじめまして"), ("person-2", "Nice to meet you"), ("person-1", "ラーメン行こう")]
    assert server.connections >= 2  # reconnected after the drop
    assert not [e for e in errors if e and "unique" in e]

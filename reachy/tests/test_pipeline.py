"""End to end without network or robot: WAV file → segmentation → scripted
recognition → echo translation → hub → WebSocket clients."""

import base64
import json
import threading
import time

import numpy as np

from reachy_mini_intermediator.audio.sources import WavFileSource
from reachy_mini_intermediator.config import Participant, Settings
from reachy_mini_intermediator.engines.asr import ScriptedASR
from reachy_mini_intermediator.engines.shisa import ShisaRealtimeASR, ShisaTranslator
from reachy_mini_intermediator.engines.translate import EchoTranslator, Turn
from reachy_mini_intermediator.hub import Hub
from reachy_mini_intermediator.pipeline import Pipeline
from reachy_mini_intermediator.session import Session

from .conftest import FakeWebSocket, conversation, write_wav


def make(tmp_path, loop_thread, asr, source=None, **overrides):
    settings = Settings(participants=[Participant("a", "Aiko", "ja", "left"),
                                      Participant("b", "Ben", "en", "right")], **overrides)
    session = Session(settings.participants)
    hub = Hub(session, info={"asr": "test"})
    hub.attach(loop_thread.loop)
    pipeline = Pipeline(settings, session, hub, source, asr, EchoTranslator())
    return settings, session, hub, pipeline


def connect(hub, loop_thread, participant=None):
    ws = FakeWebSocket(loop_thread.loop)
    loop_thread.loop.call_soon_threadsafe(lambda: loop_thread.loop.create_task(hub.serve(ws, participant)))
    ws.wait_for(lambda m: m["type"] == "hello")
    return ws


def test_two_clients_receive_live_transcripts_and_translations(tmp_path, loop_thread):
    wav = write_wav(tmp_path / "talk.wav", conversation([2.4, 2.0, 1.6]))
    asr = ScriptedASR(["ja|今日はいい天気ですね", "en|Yes, perfect for a walk", "ja|じゃあ行きましょう"])
    _, _, hub, pipeline = make(tmp_path, loop_thread, asr, WavFileSource(str(wav), speed=3.0),
                               partials="on", partial_interval=0.4, end_silence_ms=500)
    ws_a = connect(hub, loop_thread, "a")
    ws_b = connect(hub, loop_thread, "b")
    pipeline.start()
    try:
        assert pipeline.source_done.wait(15)
        assert pipeline.wait_idle(15)
        ws_b.wait_for(lambda m: m["type"] == "translation" and m["text"] == "[ja] Yes, perfect for a walk")
    finally:
        pipeline.stop()

    for ws in (ws_a, ws_b):
        finals = [m["utterance"] for m in ws.of_type("final")]
        assert [(u["speaker"], u["lang"], u["text"]) for u in finals] == [
            ("a", "ja", "今日はいい天気ですね"),
            ("b", "en", "Yes, perfect for a walk"),
            ("a", "ja", "じゃあ行きましょう"),
        ]
        assert all(u["attribution"] == "lang" for u in finals)
        translations = {(m["id"], m["lang"]): m["text"] for m in ws.of_type("translation")}
        assert translations[(finals[0]["id"], "en")] == "[en] 今日はいい天気ですね"
        assert translations[(finals[1]["id"], "ja")] == "[ja] Yes, perfect for a walk"
        # Live partials precede the final of the same utterance.
        partial_ids = {m["utterance"]["id"] for m in ws.of_type("partial")}
        assert finals[0]["id"] in partial_ids
        assert ws.of_type("status")

    # A late joiner gets the whole conversation, translations included.
    late = connect(hub, loop_thread)
    history = late.sent[0]["history"]
    assert [u["text"] for u in history if u["final"]][1] == "Yes, perfect for a walk"
    assert history[0]["translations"] == {"en": "[en] 今日はいい天気ですね"}


def test_client_commands(tmp_path, loop_thread):
    _, session, hub, pipeline = make(tmp_path, loop_thread, None)
    ws = connect(hub, loop_thread, "a")
    pipeline.start()

    ws.push({"type": "update_participant", "id": "b", "name": "Benjamin", "lang": "fr"})
    m = ws.wait_for(lambda m: m["type"] == "participants")
    assert m["participants"][1]["name"] == "Benjamin"

    ws.push({"type": "text", "participant": "a", "text": "こんにちは"})
    final = ws.wait_for(lambda m: m["type"] == "final")["utterance"]
    assert (final["speaker"], final["attribution"], final["lang"]) == ("a", "typed", "ja")
    ws.wait_for(lambda m: m["type"] == "translation" and m["lang"] == "fr")

    ws.push({"type": "reassign", "id": final["id"], "speaker": "b"})
    fixed = ws.wait_for(lambda m: m["type"] == "final" and m["utterance"]["speaker"] == "b")["utterance"]
    # Japanese said by Benjamin: Aiko reads Japanese already, nothing to translate.
    assert fixed["attribution"] == "manual" and fixed["translations"] == {}

    ws.push({"type": "ptt", "active": True})
    ws.wait_for(lambda m: m["type"] == "status" and m["status"]["ptt"] == "a")
    ws.disconnect()
    time.sleep(0.2)
    assert session.ptt_active() is None  # releasing on disconnect

    ws2 = connect(hub, loop_thread)
    ws2.push({"type": "control", "action": "pause"})
    ws2.wait_for(lambda m: m["type"] == "status" and m["status"]["paused"])
    ws2.push({"type": "control", "action": "clear"})
    ws2.wait_for(lambda m: m["type"] == "cleared")
    assert hub.transcript() == []
    pipeline.stop()


class FakeShisaSocket:
    """Plays the Shisa realtime server: answers session.update, then scripted events."""

    def __init__(self, script):
        self.sent = []
        self.script = script
        self._inbox = []
        self._cv = threading.Condition()
        self.closed = False

    def send(self, raw):
        msg = json.loads(raw)
        with self._cv:
            self.sent.append(msg)
            if msg["type"] == "session.update":
                self._inbox.append({"type": "session.created", "session_id": "s1", "seq": 1})
            if msg["type"] == "input_audio.append" and len(self.of("input_audio.append")) == 3:
                self._inbox.extend(self.script)
            self._cv.notify_all()

    def of(self, kind):
        return [m for m in self.sent if m["type"] == kind]

    def close(self):
        with self._cv:
            self.closed = True
            self._cv.notify_all()

    def __iter__(self):
        while True:
            with self._cv:
                while not self._inbox and not self.closed:
                    self._cv.wait(0.5)
                if self.closed:
                    return
                item = self._inbox.pop(0)
            yield json.dumps(item)


def test_shisa_realtime_stream(tmp_path, loop_thread):
    script = [
        {"type": "speech_started", "utterance_id": "1", "audio_start_ms": 100, "seq": 2},
        {"type": "asr.partial_result", "utterance_id": "1", "result_id": "p1", "text": "お腹",
         "is_final": False, "audio_start_ms": 100, "audio_end_ms": 400, "seq": 3},
        {"type": "asr.final_result", "utterance_id": "1", "result_id": "f1", "replaces": ["p1"],
         "text": "お腹が空いた", "language": "ja", "is_final": True, "audio_start_ms": 100,
         "audio_end_ms": 900, "seq": 4},
        {"type": "error", "code": "transient", "message": "hiccup", "fatal": False, "seq": 5},
    ]
    sock = FakeShisaSocket(script)
    headers = {}

    def connect_fn(url, hdrs):
        headers.update(hdrs)
        return sock

    asr = ShisaRealtimeASR("shsk:test", connect=connect_fn)

    class ChunkSource:
        finished = False

        def start(self):
            pass

        def read(self, timeout=0.2):
            time.sleep(0.03)
            return np.full(480, 0.25, np.float32)

        def stop(self):
            pass

        def describe(self):
            return "chunks"

    _, _, hub, pipeline = make(tmp_path, loop_thread, asr, ChunkSource())
    ws = connect(hub, loop_thread, "b")
    pipeline.start()
    try:
        part = ws.wait_for(lambda m: m["type"] == "partial")["utterance"]
        final = ws.wait_for(lambda m: m["type"] == "final")["utterance"]
        ws.wait_for(lambda m: m["type"] == "translation" and m["text"] == "[en] お腹が空いた")
        ws.wait_for(lambda m: m["type"] == "status" and "hiccup" in (m["status"]["error"] or ""))
    finally:
        pipeline.stop()

    assert headers["Authorization"] == "Bearer shsk:test"
    update = sock.of("session.update")[0]
    assert update["session"] == {"input_audio_format": "pcm_s16le", "sample_rate": 16000, "channels": 1,
                                 "language": "auto", "default_language": "ja",
                                 "language_detection_mode": "utterance"}
    chunk = base64.b64decode(sock.of("input_audio.append")[0]["audio"])
    assert 2 * 1600 <= len(chunk) <= 2 * 4000  # 100-250 ms of PCM16 mono, as Shisa recommends
    assert np.frombuffer(chunk, "<i2")[0] == int(0.25 * 32767)
    assert part["id"] == final["id"] and final["text"] == "お腹が空いた"
    assert (final["speaker"], final["attribution"]) == ("a", "lang")
    assert abs(final["end"] - final["start"] - 0.8) < 1e-6  # server ms offsets map onto wall time
    assert sock.of("session.close")


def test_shisa_translator_form(monkeypatch):
    sent = {}

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"role": "assistant", "content": " I'm hungry "}}]}

    tr = ShisaTranslator("shsk:x", keywords=lambda: ["Aiko", "Ben"])

    def request(method, url, files=None, timeout=None):
        sent.update(url=url, files=files)
        return Resp()

    monkeypatch.setattr(tr._session, "request", request)
    out = tr.translate("お腹が空いた", "ja", "en", "Aiko", [Turn("Ben", "en", "Lunch?", {"ja": "お昼？"})])
    assert out == "I'm hungry"
    assert sent["url"] == "https://api.shisa.ai/translate/"
    fields = [(k, v[1]) for k, v in sent["files"]]
    assert ("source_lang", "ja") in fields and ("target_lang", "en") in fields
    assert ("context", "Ben (English): Lunch?") in fields
    assert [v for k, v in fields if k == "keywords"] == ["Aiko", "Ben"]
    assert tr._session.headers["Authorization"] == "Bearer shsk:x"

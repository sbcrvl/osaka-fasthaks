import io
import wave
import time

import numpy as np

from reachy_mini_intermediator.engines.shisa import DEFAULT_VOICES, ShisaTTS

VOICES = {"voices": [
    {"id": "v-ja-en", "language": "Japanese & English", "gender": "male", "formats": ["mp3", "ogg", "pcm"],
     "sample_rates": [16000, 24000], "streaming": True},
    {"id": "v-en", "language": "English", "gender": "female", "formats": ["wav", "mp3"],
     "sample_rates": [24000], "streaming": False},
    {"id": "v-mp3", "language": "Japanese", "formats": ["mp3"], "sample_rates": [24000]},
]}


class Resp:
    def __init__(self, body=None, content=b"", status=200):
        self.body, self.content, self.status_code, self.text = body, content, status, ""

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


def test_voice_choice_and_wav_request(monkeypatch):
    tts = ShisaTTS("shsk:x")
    posts = []
    catalogue = {"voices": VOICES["voices"] + [
        {"id": "v-zh", "language": "Chinese", "formats": ["wav"], "sample_rates": [24000]}]}
    monkeypatch.setattr(tts._session, "request",
                        lambda method, url, **kw: Resp(catalogue) if method == "GET" else post(url, **kw))

    def post(url, json=None, timeout=None):
        posts.append((url, json))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:  # 100 ms at 24 kHz, as Shisa sends WAV
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
            w.writeframes((np.full(2400, 0.5) * 32767).astype("<i2").tobytes())
        return Resp(content=buf.getvalue())

    # The team's tested voices are used by default for Japanese and English.
    assert tts.voice_for("ja")["id"] == DEFAULT_VOICES["ja"]
    assert tts.voice_for("en")["id"] == DEFAULT_VOICES["en"]
    # Other languages come from the catalogue.
    assert tts.voice_for("zh")["id"] == "v-zh"
    audio = tts.synthesize("こんにちは", "ja")
    url, body = posts[0]
    assert url == "https://api.shisa.ai/tts"
    assert body == {"voice_id": DEFAULT_VOICES["ja"], "text": "こんにちは", "format": "wav"}
    assert audio.size == 1600 and abs(audio[800] - 0.5) < 1e-2  # resampled to 16 kHz


def test_catalogue_matching_skips_unusable_voices():
    tts = ShisaTTS("shsk:x", voices={})
    tts.pinned = {}
    tts._catalogue = VOICES["voices"]
    assert tts.voice_for("ja")["id"] == "v-ja-en"  # the mp3-only voice is skipped
    assert tts.voice_for("en")["id"] == "v-en"  # dedicated voice beats bilingual


def test_rate_limit_is_waited_out(monkeypatch):
    from reachy_mini_intermediator.engines import shisa

    calls = []
    monkeypatch.setattr(shisa.time, "sleep", lambda s: calls.append(s))

    class Session:
        def __init__(self):
            self.n = 0

        def request(self, method, url, **kw):
            self.n += 1
            if self.n < 3:
                r = Resp(status=429)
                r.text = "Too many requests. Retry after 1.72s"
                return r
            return Resp({"ok": True})

    r = shisa.shisa_request(Session(), "POST", "https://api.shisa.ai/translate/")
    assert r.status_code == 200 and calls == [1.72, 1.72]


def test_pinned_voice():
    tts = ShisaTTS("shsk:x", voices={"ja": "v-mine"})
    tts._catalogue = VOICES["voices"]
    assert tts.voice_for("ja")["id"] == "v-mine"


def test_voice_worker_mutes_mic_and_plays_in_order():
    from reachy_mini_intermediator.voice import Voice

    played, states, muted = [], [], []

    class TTS:
        def synthesize(self, text, lang):
            return np.zeros(800, np.float32)  # 50 ms

        def describe(self):
            return "tts"

    class Speaker:
        def play(self, audio):
            played.append(audio.size)

    v = Voice(TTS(), Speaker(), on_state=states.append, mute_mic=muted.append)
    v.start()
    v.say("u1", "hello", "en")
    v.say("u2", "bonjour", "fr")
    deadline = time.monotonic() + 3
    while len(played) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    v.stop()
    assert played == [800, 800]
    assert states[0] == {"id": "u1", "lang": "en"} and None in states
    assert muted and muted[0] > time.time() - 5

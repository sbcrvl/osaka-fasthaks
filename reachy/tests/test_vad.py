import numpy as np

from reachy_mini_intermediator.audio.sources import resample, to_mono
from reachy_mini_intermediator.audio.vad import Segmenter, SpeechEnd, SpeechStart

from .conftest import SR, conversation, silence, speech_like


def run(seg: Segmenter, audio: np.ndarray, chunk: int = 333) -> list:
    events, t = [], 1000.0
    for i in range(0, audio.size, chunk):
        c = audio[i : i + chunk]
        t += c.size / SR
        events += seg.feed(c, t)
    return events


def test_segments_two_turns_with_timing():
    audio = conversation([1.5, 2.0])
    events = run(Segmenter(end_silence_ms=600), audio)
    starts = [e for e in events if isinstance(e, SpeechStart)]
    ends = [e for e in events if isinstance(e, SpeechEnd)]
    assert len(starts) == 2 and len(ends) == 2
    # Turn 1 starts at 0.8 s of audio; t=1000 is the stream start.
    assert abs(starts[0].t_start - 1000.8) < 0.35
    d0 = ends[0].audio.size / SR
    assert 1.4 < d0 < 2.3, d0
    assert ends[0].t_end <= starts[1].t_start


def test_ignores_short_clicks_and_silence():
    audio = np.concatenate([silence(1.0), speech_like(0.12), silence(1.5)])
    events = run(Segmenter(min_utterance_s=0.35), audio)
    assert not [e for e in events if isinstance(e, SpeechEnd)]


def test_long_monologue_is_cut_and_continues():
    audio = np.concatenate([silence(0.5), speech_like(5.0), silence(1.2)])
    events = run(Segmenter(max_utterance_s=2.0), audio)
    ends = [e for e in events if isinstance(e, SpeechEnd)]
    assert len(ends) >= 3
    assert ends[0].forced
    assert any(isinstance(e, SpeechStart) and e.continuation for e in events)


def test_flush_closes_open_utterance():
    seg = Segmenter()
    run(seg, np.concatenate([silence(0.5), speech_like(1.0)]))
    assert seg.in_speech
    events = seg.flush(2000.0)
    assert len(events) == 1 and isinstance(events[0], SpeechEnd)


def test_resample_and_mono():
    stereo = np.stack([np.ones(480), -np.ones(480)], axis=1).astype(np.float32)
    assert np.allclose(to_mono(stereo), 0)
    assert resample(np.zeros(4800, np.float32), 48000).size == 1600

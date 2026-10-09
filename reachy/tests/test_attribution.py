import math

from reachy_mini_intermediator.config import Participant
from reachy_mini_intermediator.engines.asr import ASRResult
from reachy_mini_intermediator.session import Session
from reachy_mini_intermediator.speakers import Attributor, DoATracker


def people(lang_a="ja", lang_b="en"):
    return Session([Participant("a", "Aiko", lang_a, "left"), Participant("b", "Ben", lang_b, "right")])


class FakeASR:
    """Records the language hint and 'hears' a fixed language."""

    def __init__(self, heard: str):
        self.heard = heard
        self.calls = []

    def __call__(self, language, candidates):
        self.calls.append((language, tuple(candidates)))
        return ASRResult("hello", language or self.heard)


def test_push_to_talk_wins_and_forces_language():
    s = people()
    s.set_ptt("b", True, now=100.0)
    s.set_ptt("b", False, now=103.0)
    asr = FakeASR("ja")
    attr, res = Attributor(s).resolve(100.5, 102.5, asr)
    assert attr.speaker == "b" and attr.method == "ptt"
    assert asr.calls == [("en", ("ja", "en"))]


def test_language_identifies_speaker():
    s = people()
    asr = FakeASR("en")
    attr, res = Attributor(s).resolve(10, 12, asr)
    assert (attr.speaker, attr.method, res.language) == ("b", "lang", "en")
    assert asr.calls == [(None, ("ja", "en"))]


def test_direction_when_both_speak_the_same_language():
    s = people("en", "en")
    doa = DoATracker()
    for i in range(20):
        doa.add(50 + i * 0.1, 2.6, True)  # right side (angle > pi/2)
    asr = FakeASR("en")
    attr, _ = Attributor(s, doa=doa).resolve(50, 52, asr)
    assert (attr.speaker, attr.method) == ("b", "doa")
    assert asr.calls == [("en", ("en",))]


def test_ambiguous_direction_falls_back_to_turn_taking():
    s = people("en", "en")
    doa = DoATracker()
    for i in range(20):
        doa.add(50 + i * 0.1, 0.4 if i % 2 else 2.6, True)
    att = Attributor(s, doa=doa)
    first, _ = att.resolve(50, 52, FakeASR("en"))
    second, _ = att.resolve(60, 62, FakeASR("en"))
    assert first.method == "turn" and {first.speaker, second.speaker} == {"a", "b"}


def test_partials_do_not_advance_turns():
    s = people("en", "en")
    att = Attributor(s, mode="turn")
    att.resolve(0, 1, FakeASR("en"), commit=False)
    assert att.last_speaker is None


def test_doa_side_convention_and_split():
    d = DoATracker()
    assert d.side_of(0.1) == "left" and d.side_of(math.pi - 0.1) == "right"
    d2 = DoATracker(split=1.0)
    assert d2.side_of(1.2) == "right"


def test_changing_side_swaps_the_other_participant():
    s = people()
    s.update_participant("a", side="right")
    assert s.participant("b").side == "left"


def test_env_files_fill_in_missing_keys(tmp_path, monkeypatch):
    from reachy_mini_intermediator.config import Settings, read_env_files

    (tmp_path / "shisa").mkdir()
    (tmp_path / "reachy").mkdir()
    (tmp_path / "shisa" / ".env").write_text("# key\nSHISA_API_KEY=shsk:from-file\n")
    found = read_env_files(base=str(tmp_path / "reachy"))
    assert found == {"SHISA_API_KEY": "shsk:from-file"}
    monkeypatch.chdir(tmp_path / "reachy")
    monkeypatch.delenv("SHISA_API_KEY", raising=False)
    assert Settings.from_args([]).shisa_api_key == "shsk:from-file"
    monkeypatch.setenv("SHISA_API_KEY", "shsk:from-env")
    assert Settings.from_args([]).shisa_api_key == "shsk:from-env"

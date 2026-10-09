"""Robot-facing code against a stand-in for the reachy_mini SDK."""

import sys
import threading
import time
import types

import numpy as np
import pytest


class FakeMedia:
    def __init__(self):
        self.recording = False
        self.pushed = []
        self.chunks = [np.full((800, 2), 0.1, np.float32), None, np.full((320, 2), -0.2, np.float32)]

    def start_recording(self):
        self.recording = True

    def stop_recording(self):
        self.recording = False

    def get_input_audio_samplerate(self):
        return 16000

    def get_output_audio_samplerate(self):
        return 16000

    def get_audio_sample(self):
        return self.chunks.pop(0) if self.chunks else None

    def start_playing(self):
        pass

    def push_audio_sample(self, data):
        self.pushed.append(data.size)


class FakeMini:
    def __init__(self, *a, **kw):
        self.media = FakeMedia()
        self.gotos = []
        self.kwargs = kw

    def goto_target(self, head=None, antennas=None, duration=0.5, body_yaw=0.0, **kw):
        self.gotos.append({"head": head, "antennas": antennas, "duration": duration, "body_yaw": body_yaw})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


@pytest.fixture
def fake_sdk(monkeypatch):
    sdk = types.ModuleType("reachy_mini")
    utils = types.ModuleType("reachy_mini.utils")

    def create_head_pose(x=0, y=0, z=0, roll=0, pitch=0, yaw=0, mm=False, degrees=True):
        return {"roll": roll, "pitch": pitch, "yaw": yaw}

    utils.create_head_pose = create_head_pose

    class ReachyMiniApp:
        def __init__(self):
            self.stop_event = threading.Event()

    sdk.ReachyMini = FakeMini
    sdk.ReachyMiniApp = ReachyMiniApp
    sdk.utils = utils
    monkeypatch.setitem(sys.modules, "reachy_mini", sdk)
    monkeypatch.setitem(sys.modules, "reachy_mini.utils", utils)
    sys.modules.pop("reachy_mini_intermediator.main", None)
    return sdk


def test_robot_microphone_is_mono_16k(fake_sdk):
    from reachy_mini_intermediator.audio.sources import ReachyMiniSource

    mini = FakeMini()
    src = ReachyMiniSource(mini)
    src.start()
    assert mini.media.recording
    a = src.read()
    assert a.shape == (800,) and abs(a[0] - 0.1) < 1e-6
    assert src.read().shape == (320,)  # waits through the empty poll
    src.stop()
    assert not mini.media.recording


def test_gestures_look_at_speaker_then_listener(fake_sdk):
    from reachy_mini_intermediator.robot import Gestures

    mini = FakeMini()
    g = Gestures(mini, look_yaw_deg=30)
    g.start()
    time.sleep(0.1)
    g.listen_to("left")
    time.sleep(0.2)
    g.relay_to("right")
    time.sleep(0.3)
    g.stop()
    g.join(2)
    yaws = [m["head"]["yaw"] for m in mini.gotos]
    assert yaws[0] == 0  # neutral on start
    assert 30 in yaws and -30 in yaws
    assert yaws.index(30) < yaws.index(-30)
    assert all(m["body_yaw"] == 0.0 for m in mini.gotos)


def test_robot_speaker_pushes_100ms_chunks(fake_sdk):
    from reachy_mini_intermediator.voice import RobotSpeaker

    mini = FakeMini()
    duration = RobotSpeaker(mini).play(np.zeros(4000, np.float32))
    assert mini.media.pushed == [1600, 1600, 800] and duration == 0.25


def test_app_entry_point_runs_server_until_stopped(fake_sdk, monkeypatch, tmp_path):
    from reachy_mini_intermediator import runtime as rt

    served = {}

    def fake_serve(self, stop_event=None):
        served["settings"] = self.settings
        served["mini"] = self.mini
        stop_event.wait(2)

    monkeypatch.setattr(rt.Runtime, "serve", fake_serve)
    monkeypatch.setenv("INTERMEDIATOR_A", "Aiko:ja:left")
    monkeypatch.delenv("INTERMEDIATOR_GESTURES", raising=False)
    from reachy_mini_intermediator.main import ReachyMiniIntermediator

    app = ReachyMiniIntermediator()
    mini = FakeMini()
    t = threading.Thread(target=app.run, args=(mini, app.stop_event))
    t.start()
    time.sleep(0.2)
    app.stop_event.set()
    t.join(3)
    s = served["settings"]
    assert served["mini"] is mini
    assert s.source == "reachy" and s.gestures is True
    assert s.participants[0].name == "Aiko"

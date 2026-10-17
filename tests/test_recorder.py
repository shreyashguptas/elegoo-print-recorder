import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from elegoo_print_recorder import core
from fake_printer import FakePrinter

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")


def wait_for(cond, timeout, step=0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True).stdout
    return float(json.loads(out)["format"]["duration"])


@pytest.fixture
def setup(tmp_path):
    fake = FakePrinter(FFMPEG or "ffmpeg")
    cfg = dict(core.DEFAULTS, printer_ip="127.0.0.1", ws_port=fake.port, output_dir=str(tmp_path / "out"),
               poll_seconds=0.5, stop_delay_seconds=2, start_at_layer=3, stall_seconds=4,
               offline_end_seconds=5, ffmpeg=FFMPEG or "", work_dir=str(tmp_path / "work"))
    rec = core.Recorder(cfg)
    t = threading.Thread(target=rec.run, daemon=True)
    yield fake, rec, t, Path(cfg["output_dir"])
    rec.stop_event.set()
    t.join(60)
    fake.close()


def mp4s(out):
    return sorted(out.glob("*.mp4")) if out.exists() else []


@needs_ffmpeg
def test_records_a_whole_print(setup):
    fake, rec, t, out = setup
    t.start()
    time.sleep(1.5)
    assert fake.video_calls() == []            # idle: camera untouched
    fake.start_print("My Part.goo")
    time.sleep(1.5)
    assert fake.video_calls() == []            # layers 1-2: wait
    for _ in range(2):
        fake.next_layer()
    assert wait_for(lambda: fake.video_calls() == [1], 5)
    time.sleep(6)                              # record a while
    fake.finish_print()
    time.sleep(1)
    assert fake.video_calls() == [1]           # still recording during the stop delay
    assert wait_for(lambda: mp4s(out), 30)
    files = mp4s(out)
    assert len(files) == 1 and "My Part" in files[0].name
    assert duration(files[0]) > 5
    assert fake.video_calls() == [1, 0]        # camera handed back
    assert fake.viewers == 0
    assert not list(out.glob("*.ts")) and not list((out.parent / "work").glob("*.ts"))  # segments cleaned up


@needs_ffmpeg
def test_recovers_when_the_stream_stalls(setup):
    fake, rec, t, out = setup
    fake.start_print()
    fake.layer = 5
    t.start()
    assert wait_for(lambda: fake.video_calls() == [1], 5)
    time.sleep(4)
    fake.drop_video()                          # camera stops sending
    assert wait_for(lambda: fake.video_calls() == [1, 0, 1], 30)
    time.sleep(5)
    fake.finish_print()
    assert wait_for(lambda: mp4s(out), 40)
    assert duration(mp4s(out)[0]) > 6          # both parts joined
    assert fake.viewers == 0


@needs_ffmpeg
def test_shutdown_mid_print_saves_the_video(setup):
    fake, rec, t, out = setup
    fake.start_print()
    fake.layer = 10
    t.start()
    assert wait_for(lambda: fake.video_calls() == [1], 5)
    time.sleep(5)
    rec.stop_event.set()
    t.join(60)
    assert len(mp4s(out)) == 1
    assert fake.video_calls() == [1, 0]
    assert fake.viewers == 0


@needs_ffmpeg
def test_full_camera_is_left_alone(setup):
    fake, rec, t, out = setup
    fake.viewers = fake.max_viewers            # e.g. stuck slots
    fake.start_print()
    fake.layer = 10
    t.start()
    time.sleep(3)
    assert fake.video_calls() == []            # never asks, never adds a viewer
    assert fake.viewers == fake.max_viewers


@needs_ffmpeg
def test_new_print_during_stop_delay_starts_a_new_file(setup):
    fake, rec, t, out = setup
    rec.cfg["stop_delay_seconds"] = 30
    fake.start_print("First.goo", "t1")
    fake.layer = 5
    t.start()
    assert wait_for(lambda: fake.video_calls() == [1], 5)
    time.sleep(4)
    fake.finish_print()
    time.sleep(1.5)
    fake.start_print("Second.goo", "t2")
    fake.layer = 5
    assert wait_for(lambda: len(mp4s(out)) == 1, 30)
    assert "First" in mp4s(out)[0].name
    assert wait_for(lambda: fake.video_calls() == [1, 0, 1], 10)


def test_check_ip():
    assert core.check_ip("192.168.1.20") is None
    assert core.check_ip("10.0.0.5") is None
    assert core.check_ip("8.8.8.8")
    assert core.check_ip("127.0.0.1")
    assert core.check_ip("192.168.1")
    assert core.check_ip("printer.local")


def test_print_state():
    st = core.print_state({"CurrentStatus": [1], "PrintInfo": {"Status": 3, "CurrentLayer": 4, "TotalLayer": 9,
                                                                "Filename": "a.goo", "TaskId": "x"}})
    assert st["printing"] and st["layer"] == 4 and st["task_id"] == "x"
    assert not core.print_state({"CurrentStatus": [0], "PrintInfo": {"Status": 9}})["printing"]
    assert not core.print_state({"CurrentStatus": [1], "PrintInfo": {"Status": 8}})["printing"]
    assert not core.print_state({})["printing"]


def test_safe_name():
    assert core.safe_name("OG-3_P_v2.1_-_CCW.goo") == "OG-3_P_v2.1_-_CCW"
    assert core.safe_name("../../etc/passwd") == "etc_passwd"
    assert core.safe_name("") == "print"


def _camera_like_ts(tmp_path):
    """A short .ts with the empty NAL units the Elegoo camera sends."""
    raw = tmp_path / "clean.h264"
    subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30", "-t", "3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-profile:v", "high", "-bf", "0", "-g", "30", "-f", "h264", str(raw)], check=True)
    sc = b"\x00\x00\x00\x01"
    bad = tmp_path / "camera.h264"
    bad.write_bytes(raw.read_bytes().replace(sc, sc + sc))   # start code, nothing, start code
    ts = tmp_path / "x.part01.ts"
    subprocess.run([FFMPEG, "-v", "error", "-use_wallclock_as_timestamps", "1", "-f", "h264", "-i", str(bad),
                    "-c", "copy", "-f", "mpegts", str(ts)], check=True)
    return ts


@needs_ffmpeg
def test_mp4_from_camera_stream_is_clean(tmp_path):
    ts = _camera_like_ts(tmp_path)
    out = core.finalize(FFMPEG, [ts], tmp_path / "print.mp4", keep_ts=False)
    assert out and out.exists() and not ts.exists()
    errors = subprocess.run([FFMPEG, "-v", "error", "-i", str(out), "-fps_mode", "passthrough",
                             "-enc_time_base", "demux", "-f", "null", "-"], capture_output=True, text=True).stderr
    assert errors.strip() == ""
    pts = [int(x) for x in subprocess.run(["ffprobe", "-v", "error", "-show_entries", "packet=pts", "-of", "csv=p=0",
                                           str(out)], capture_output=True, text=True).stdout.split()]
    steps = {b - a for a, b in zip(pts, pts[1:])}
    assert len(pts) > 30 and max(steps) - min(steps) <= 1      # evenly spaced frames
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
         "stream=codec_tag_string,width,height", "-of", "json", str(out)],
        capture_output=True, text=True).stdout)["streams"][0]
    assert info == {"codec_tag_string": "avc1", "width": 1280, "height": 720}


@needs_ffmpeg
def test_interrupted_recording_is_finished_on_restart(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    part = _camera_like_ts(tmp_path)
    part = part.rename(work / "s1.part01.ts")
    out = tmp_path / "out"
    out.mkdir()
    (work / "session.json").write_text(json.dumps({"base": str(out / "Old Print.mp4"), "parts": [str(part)]}))
    fake = FakePrinter(FFMPEG)
    cfg = dict(core.DEFAULTS, printer_ip="127.0.0.1", ws_port=fake.port, output_dir=str(out), poll_seconds=0.5,
               ffmpeg=FFMPEG, work_dir=str(work))
    rec = core.Recorder(cfg)
    t = threading.Thread(target=rec.run, daemon=True)
    t.start()
    assert wait_for(lambda: (out / "Old Print.mp4").exists(), 20)
    assert wait_for(lambda: not (work / "session.json").exists(), 5)
    rec.stop_event.set()
    t.join(30)
    fake.close()


def test_same_name_does_not_overwrite(tmp_path):
    a = tmp_path / "x.mp4"
    a.write_text("first")
    src = tmp_path / "new"
    src.write_text("second")
    dest = core.move_into(src, a, None)
    assert dest.name == "x (2).mp4" and a.read_text() == "first"

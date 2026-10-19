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
               offline_end_seconds=5, ffmpeg=FFMPEG or "", work_dir=str(tmp_path / "work"), mode="video")
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
    assert len(files) == 1 and files[0].name.endswith("_My_Part.mp4")
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
    assert wait_for(lambda: rec.segment is not None and rec.segment.size() > 200_000, 40)   # real video, not a fixed wait
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
               ffmpeg=FFMPEG, work_dir=str(work), mode="video")
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
    assert dest.name == "x_2.mp4" and a.read_text() == "first"


@needs_ffmpeg
def test_nas_offline_keeps_video_and_retries(tmp_path, monkeypatch):
    part = _camera_like_ts(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    part = part.rename(work / "s2.part01.ts")
    out = tmp_path / "nas" / "Print.mp4"          # the "NAS" folder does not exist yet
    real_move = core.move_into
    calls = {"n": 0}

    def flaky(src, dest, cfg):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("NAS offline")
        return real_move(src, dest, cfg)

    monkeypatch.setattr(core, "move_into", flaky)
    with pytest.raises(OSError):
        core.finalize(FFMPEG, [part], out, keep_ts=False)
    assert part.exists()                            # nothing lost
    assert (work / "s2.partial.done").exists()
    final = core.finalize(FFMPEG, [part], out, keep_ts=False)   # the retry
    assert final and final.exists() and not part.exists()


class AlertInbox:
    """A tiny local stand-in for ntfy that records every alert."""

    def __init__(self):
        import http.server
        inbox = self.alerts = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                inbox.append((self.headers.get("Title"), self.headers.get("Priority"), body))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}/topic"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def titles(self):
        return [a[0] for a in self.alerts]


@needs_ffmpeg
def test_alerts_when_camera_full_and_when_recording_starts(setup):
    fake, rec, t, out = setup
    inbox = AlertInbox()
    rec.cfg["notify_url"] = inbox.url
    fake.viewers = fake.max_viewers                 # stuck viewers
    fake.start_print("Alert Part.goo")
    fake.layer = 5
    t.start()
    assert wait_for(lambda: "Print is NOT being recorded" in inbox.titles(), 10)
    time.sleep(2)
    assert inbox.titles().count("Print is NOT being recorded") == 1   # once, not every poll
    fake.viewers = 0                                # a slot frees up
    rec.next_stream_try = 0
    assert wait_for(lambda: "Recording started" in inbox.titles(), 15)
    time.sleep(4)
    fake.finish_print()
    assert wait_for(lambda: "Print video saved" in inbox.titles(), 40)


@needs_ffmpeg
def test_alert_when_nothing_recorded_and_when_idle_camera_full(setup):
    fake, rec, t, out = setup
    inbox = AlertInbox()
    rec.cfg.update(notify_url=inbox.url, idle_check_minutes=0)
    fake.viewers = fake.max_viewers
    fake.start_print("Lost Part.goo")
    fake.layer = 5
    t.start()
    assert wait_for(lambda: "Print is NOT being recorded" in inbox.titles(), 10)
    fake.finish_print()
    assert wait_for(lambda: "Print was NOT recorded" in inbox.titles(), 15)
    assert wait_for(lambda: "Next print won't be recorded" in inbox.titles(), 10)
    fake.viewers = 0
    assert wait_for(lambda: "Camera is free again" in inbox.titles(), 10)
    assert all(a[1] == "high" for a in inbox.alerts if "NOT" in a[0] or "won't" in a[0])



# ---------------------------------------------------------------- printer timelapses

def _small_mp4(tmp_path):
    f = tmp_path / "tl.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30", "-t", "2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(f)], check=True)
    return f.read_bytes()


@pytest.fixture
def tl(tmp_path):
    fake = FakePrinter(FFMPEG or "ffmpeg")
    inbox = AlertInbox()
    cfg = dict(core.DEFAULTS, printer_ip="127.0.0.1", ws_port=fake.port, output_dir=str(tmp_path / "nas"),
               poll_seconds=0.3, ffmpeg=FFMPEG or "", work_dir=str(tmp_path / "work"), mode="timelapse",
               files_url=fake.files_url, notify_url=inbox.url)
    rec = core.Recorder(cfg)
    t = threading.Thread(target=rec.run, daemon=True)
    yield fake, rec, t, Path(cfg["output_dir"]), inbox
    rec.stop_event.set()
    t.join(30)
    fake.close()


@needs_ffmpeg
def test_copies_existing_timelapses_once_and_never_uses_camera(tl, tmp_path):
    fake, rec, t, out, inbox = tl
    clip = _small_mp4(tmp_path)
    fake.add_history("Old Part.goo", "aaaa1111", 1, clip, ago=3600)
    fake.add_history("Stopped.goo", "bbbb2222", 4, ago=1800, task_status=3)
    fake.add_history("Newest Part.goo", "cccc3333", 1, clip, ago=600)
    t.start()
    assert wait_for(lambda: len(list(out.glob("*_timelapse.mp4"))) == 2, 15)
    names = sorted(p.name for p in out.glob("*.mp4"))
    assert any(n.endswith("_Old_Part_timelapse.mp4") for n in names)
    assert any(n.endswith("_Newest_Part_timelapse.mp4") for n in names)
    assert all(n[:4].isdigit() for n in names)           # real dates from the printer's clock
    assert all(f.stat().st_size == len(clip) for f in out.glob("*.mp4"))
    rec.next_tl_sync = 0
    time.sleep(2)
    assert len(list(out.glob("*.mp4"))) == 2              # not copied twice
    assert fake.video_calls() == []                       # camera never touched
    assert not list((tmp_path / "work").glob("*.download.mp4"))


@needs_ffmpeg
def test_waits_for_a_timelapse_being_made_after_a_print(tl, tmp_path):
    fake, rec, t, out, inbox = tl
    clip = _small_mp4(tmp_path)
    fake.start_print("Fresh.goo", "dddd4444")
    fake.layer = 50
    t.start()
    time.sleep(1.5)
    fake.finish_print()
    fake.add_history("Fresh.goo", "dddd4444", 3, ago=60)    # printer still writing it
    time.sleep(2)
    assert not list(out.glob("*.mp4")) if out.exists() else True
    fake.history[0]["TimeLapseVideoStatus"] = 1
    fake.files[fake.history[0]["TimeLapseVideoUrl"]] = clip
    rec.next_tl_sync = 0
    assert wait_for(lambda: out.exists() and len(list(out.glob("*_Fresh_timelapse.mp4"))) == 1, 40)
    assert wait_for(lambda: "Print timelapse saved" in inbox.titles(), 5)


@needs_ffmpeg
def test_broken_download_is_retried_not_saved(tl, tmp_path):
    fake, rec, t, out, inbox = tl
    fake.add_history("Bad.goo", "eeee5555", 1, b"not a video at all", ago=60)
    t.start()
    time.sleep(3)
    assert not (out.exists() and list(out.glob("*.mp4")))
    assert "eeee5555" not in rec.timelapses.load()        # still pending, will retry
    fake.files[fake.history[0]["TimeLapseVideoUrl"]] = _small_mp4(tmp_path)
    rec.next_tl_sync = 0
    assert wait_for(lambda: out.exists() and len(list(out.glob("*_Bad_timelapse.mp4"))) == 1, 15)


# ---------------------------------------------------------------- failure drills

def _new_rec(fake, tmp_path, **kw):
    cfg = dict(core.DEFAULTS, printer_ip="127.0.0.1", ws_port=fake.port, output_dir=str(tmp_path / "nas"),
               poll_seconds=0.3, ffmpeg=FFMPEG or "", work_dir=str(tmp_path / "work"), mode="timelapse",
               files_url=fake.files_url)
    cfg.update(kw)
    rec = core.Recorder(cfg)
    t = threading.Thread(target=rec.run, daemon=True)
    t.start()
    return rec, t


def _stop(rec, t):
    rec.stop_event.set()
    t.join(30)


def _mp4s(tmp_path):
    d = tmp_path / "nas"
    return sorted(p.name for p in d.glob("*.mp4")) if d.exists() else []


@needs_ffmpeg
def test_nas_offline_then_back(tmp_path, monkeypatch):
    fake = FakePrinter(FFMPEG)
    fake.add_history("Part.goo", "t1", 1, _small_mp4(tmp_path), ago=60)
    real = core.check_destination
    nas_up = {"v": False}

    def check(out):
        if not nas_up["v"]:
            raise OSError("NAS offline")
        real(out)

    monkeypatch.setattr(core, "check_destination", check)
    rec, t = _new_rec(fake, tmp_path)
    time.sleep(2)
    assert _mp4s(tmp_path) == [] and "t1" not in rec.timelapses.load()     # waits, nothing lost
    nas_up["v"] = True
    rec.next_tl_sync = 0
    assert wait_for(lambda: len(_mp4s(tmp_path)) == 1, 15)
    _stop(rec, t); fake.close()


@needs_ffmpeg
def test_dell_restart_and_lost_records_never_duplicate_or_resurrect(tmp_path):
    fake = FakePrinter(FFMPEG)
    clip = _small_mp4(tmp_path)
    fake.add_history("A.goo", "t1", 1, clip, ago=600)
    rec, t = _new_rec(fake, tmp_path)
    assert wait_for(lambda: len(_mp4s(tmp_path)) == 1, 15)
    _stop(rec, t)                                       # Dell restarts...
    fake.add_history("B.goo", "t2", 1, clip, ago=60)    # ...and a print finished meanwhile
    rec, t = _new_rec(fake, tmp_path)
    assert wait_for(lambda: len(_mp4s(tmp_path)) == 2, 15)
    _stop(rec, t)
    # You delete one video from the NAS, and the Dell also loses its own records.
    (tmp_path / "nas" / [n for n in _mp4s(tmp_path) if "_A_" in n][0]).unlink()
    (tmp_path / "work" / "timelapses.json").unlink()
    rec, t = _new_rec(fake, tmp_path)
    time.sleep(3)
    assert len(_mp4s(tmp_path)) == 1 and "_B_" in _mp4s(tmp_path)[0]       # deleted one stays deleted
    _stop(rec, t); fake.close()


@needs_ffmpeg
def test_printer_restart_keeps_real_dates(tmp_path):
    fake = FakePrinter(FFMPEG)
    rec, t = _new_rec(fake, tmp_path)
    fake.start_print("Dated.goo", "t9")
    fake.layer = 40
    time.sleep(1.5)                                     # the Dell notes the real start time
    _stop(rec, t)
    fake.finish_print()
    fake.booted = time.time() + 999999                  # printer restarts: its clock starts over
    fake.add_history("Dated.goo", "t9", 1, _small_mp4(tmp_path), ago=0)
    rec, t = _new_rec(fake, tmp_path)
    assert wait_for(lambda: len(_mp4s(tmp_path)) == 1, 15)
    assert _mp4s(tmp_path)[0].startswith(dt_today())    # not "undated"
    _stop(rec, t); fake.close()


def dt_today():
    import datetime
    return datetime.date.today().isoformat()


@needs_ffmpeg
def test_bad_copy_on_nas_is_removed_and_redone(tmp_path, monkeypatch):
    fake = FakePrinter(FFMPEG)
    fake.add_history("C.goo", "t3", 1, _small_mp4(tmp_path), ago=60)
    real = core.sha256_of
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        return "corrupt" if calls["n"] == 1 else real(path)

    monkeypatch.setattr(core, "sha256_of", flaky)
    rec, t = _new_rec(fake, tmp_path)
    time.sleep(2)
    assert _mp4s(tmp_path) == []                        # the bad copy was deleted, not kept
    rec.next_tl_sync = 0
    assert wait_for(lambda: len(_mp4s(tmp_path)) == 1, 15)
    lines = (tmp_path / "nas" / ".copied-timelapses.csv").read_text().splitlines()
    assert len(lines) == 1 and lines[0].startswith("t3,")
    _stop(rec, t); fake.close()


@needs_ffmpeg
def test_printer_offline_then_back(tmp_path):
    fake = FakePrinter(FFMPEG)
    fake.add_history("D.goo", "t4", 1, _small_mp4(tmp_path), ago=60)
    rec, t = _new_rec(fake, tmp_path, ws_port=1)        # printer unreachable
    time.sleep(2)
    assert _mp4s(tmp_path) == []
    rec.printer.port = rec.cfg["ws_port"] = fake.port   # printer back
    rec.next_tl_sync = 0
    assert wait_for(lambda: len(_mp4s(tmp_path)) == 1, 20)
    _stop(rec, t); fake.close()


# ---------------------------------------------------------------- printer cleanup

def _days_ago(n):
    import datetime
    return (datetime.datetime.now() - datetime.timedelta(days=n)).isoformat(timespec="minutes")


@needs_ffmpeg
def test_cleanup_removes_only_old_and_safe_things(tmp_path):
    fake = FakePrinter(FFMPEG)
    clip = _small_mp4(tmp_path)
    fake.local = ["/local//Old.goo", "/local//Recent.goo", "/local//Printing.goo"]
    fake.add_history("Old.goo", "old1", 1, clip, ago=600)        # copied, but "40 days old" (below)
    fake.add_history("Recent.goo", "new1", 1, clip, ago=300)
    work = tmp_path / "work"
    work.mkdir()
    (work / "printer-files-seen.json").write_text(json.dumps(
        {"Old.goo": _days_ago(40), "Recent.goo": _days_ago(2), "Printing.goo": _days_ago(40)}))
    (work / "print-starts.json").write_text(json.dumps({"old1": _days_ago(40), "new1": _days_ago(2)}))
    rec, t = _new_rec(fake, tmp_path)
    assert wait_for(lambda: len(_mp4s(tmp_path)) == 2, 15)            # both timelapses copied first
    rec.cleanup.next_run = 0
    rec.tl_watch_until = 0
    rec.next_tl_sync = 0
    assert wait_for(lambda: "/local//Old.goo" not in fake.local, 15)
    time.sleep(1)
    assert "/local//Recent.goo" in fake.local
    old_url = [h for h in fake.history if h["TaskId"] == "old1"][0]["TimeLapseVideoUrl"]
    new_url = [h for h in fake.history if h["TaskId"] == "new1"][0]["TimeLapseVideoUrl"]
    assert old_url not in fake.files and new_url in fake.files     # only the old, copied timelapse
    assert len(_mp4s(tmp_path)) == 2                                # NAS copies untouched
    _stop(rec, t); fake.close()


@needs_ffmpeg
def test_cleanup_never_runs_while_printing_and_keeps_uncopied(tmp_path):
    fake = FakePrinter(FFMPEG)
    fake.local = ["/local//Printing.goo"]
    fake.add_history("Uncopied.goo", "u1", 1, b"not a playable video", ago=60)
    work = tmp_path / "work"
    work.mkdir()
    (work / "printer-files-seen.json").write_text(json.dumps({"Printing.goo": _days_ago(90)}))
    (work / "print-starts.json").write_text(json.dumps({"u1": _days_ago(90)}))
    fake.start_print("Printing.goo", "p1")
    fake.layer = 20
    rec, t = _new_rec(fake, tmp_path)
    time.sleep(3)
    assert fake.local == ["/local//Printing.goo"]                  # nothing while printing
    fake.finish_print()
    assert wait_for(lambda: rec.was_printing is False, 10)
    rec.cleanup.next_run = 0
    rec.tl_watch_until = 0
    rec.next_tl_sync = 0
    assert wait_for(lambda: fake.local == [], 15)                   # old file removed once idle
    assert [h for h in fake.history if h["TaskId"] == "u1"][0]["TimeLapseVideoUrl"] in fake.files  # never copied: kept
    _stop(rec, t); fake.close()


def test_start_time_uses_elapsed_print_time(tmp_path):
    rec = types_ns(work=tmp_path)
    tl = core.TimelapseSync(rec)
    tl.note_start("x", elapsed=3 * 3600)
    import datetime
    got = datetime.datetime.fromisoformat(json.loads((tmp_path / "print-starts.json").read_text())["x"])
    assert abs((datetime.datetime.now() - got).total_seconds() - 3 * 3600) < 120


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)

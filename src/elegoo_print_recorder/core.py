#!/usr/bin/env python3
"""Elegoo Print Recorder: record the camera of an Elegoo resin printer
(Saturn 4 Ultra and other SDCP V3 printers) for every print, automatically.

It watches the printer over its local SDCP WebSocket (port 3030). When a print
starts it turns the camera stream on, saves the native H.264 video with ffmpeg
(no re-encoding), and a few minutes after the print ends it stops cleanly,
hands the camera slot back to the printer, and writes one .mp4 per print.

Standard library only (Python 3.9+). Needs ffmpeg.
"""

import argparse
import base64
import datetime as dt
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

APP = "elegoo-print-recorder"
VERSION = "0.1.0"
# The background service runs as a system LaunchDaemon (macOS exempts those from
# Local Network privacy, which silently blocks per-user agents), so its files
# live in the system Library.
SUPPORT_DIR = Path("/Library/Application Support") / APP
LOG_DIR = Path("/Library/Logs") / APP
DEFAULT_CONFIG = SUPPORT_DIR / "config.json"

DEFAULTS = {
    "printer_ip": "",              # filled in by `setup` (printer discovery)
    "mainboard_id": "",            # lets us find the printer again if its IP changes
    "output_dir": str(Path.home() / "Movies" / "Printer Recordings"),
    "stop_delay_seconds": 120,     # keep recording this long after a print ends
    "start_at_layer": 3,           # see README: asking for video in layers 1-2 can wedge the camera
    "poll_seconds": 5,
    "stall_seconds": 45,           # restart ffmpeg if the file stops growing this long
    "offline_end_seconds": 90,     # printer unreachable this long while recording = print over
    "keep_ts": False,              # also keep the raw .ts recording next to the .mp4
    "work_dir": "",                # where segments are written while recording (auto)
    "ffmpeg": "",                  # auto-detected when empty
    "ws_port": 3030,
    "discovery_port": 3000,
    "owner_uid": -1,               # recordings are handed to this user (the service runs as root)
    "owner_gid": -1,
}

log = logging.getLogger(APP)

# SDCP V3 machine states (Status.CurrentStatus) and print states (PrintInfo.Status).
MACHINE_PRINTING = 1
PRINT_IDLE, PRINT_STOPPED, PRINT_COMPLETE = 0, 8, 9
PRINT_ENDED = {PRINT_IDLE, PRINT_STOPPED, PRINT_COMPLETE}


# --------------------------------------------------------------------------- config

def load_config(path: Path) -> Dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        with open(path) as f:
            cfg.update(json.load(f))
    cfg["output_dir"] = os.path.expanduser(cfg["output_dir"])
    return cfg


def save_config(path: Path, cfg: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def give_to_owner(cfg: Dict, path: Path) -> None:
    uid, gid = int(cfg.get("owner_uid", -1)), int(cfg.get("owner_gid", -1))
    if os.geteuid() == 0 and uid >= 0:
        try:
            os.chown(path, uid, gid, follow_symlinks=False)
        except OSError as e:
            log.debug("chown %s failed: %s", path, e)


def make_output_dir(cfg: Dict) -> Path:
    out = Path(cfg["output_dir"])
    missing = []
    p = out
    while not p.exists():
        missing.append(p)
        p = p.parent
    out.mkdir(parents=True, exist_ok=True)
    for m in missing:
        give_to_owner(cfg, m)
    return out


def firewall_blocks(binary: str) -> bool:
    """True if the macOS firewall is on and would block this binary's incoming video."""
    fw = "/usr/libexec/ApplicationFirewall/socketfilterfw"
    if not os.path.exists(fw):
        return False
    state = subprocess.run([fw, "--getglobalstate"], capture_output=True, text=True).stdout.lower()
    if "enabled" not in state:
        return False
    r = subprocess.run([fw, "--getappblocked", os.path.realpath(binary)], capture_output=True, text=True)
    out = (r.stdout + r.stderr).lower()
    return not ("permitted" in out or "is not blocked" in out)


def work_dir(cfg: Dict) -> Path:
    """Private folder for in-progress segments (root-owned when run as the service,
    so nothing in the user's folders is written by root until a file is finished)."""
    if cfg.get("work_dir"):
        return Path(cfg["work_dir"])
    if os.geteuid() == 0:
        return SUPPORT_DIR / "work"
    return Path(cfg["output_dir"]) / ".recording"


def find_ffmpeg(cfg: Dict) -> str:
    # Prefer stable paths (Homebrew's bin symlink) over versioned Cellar paths,
    # which disappear on `brew upgrade`.
    candidates = [cfg.get("ffmpeg") or "", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg",
                  shutil.which("ffmpeg") or ""]
    for c in candidates:
        if c and os.access(c, os.X_OK):
            return c
    raise SystemExit("ffmpeg not found. Install it with: brew install ffmpeg")


def setup_logging(verbose: bool, to_file: bool) -> None:
    to_file = to_file and os.access(LOG_DIR if LOG_DIR.exists() else LOG_DIR.parent, os.W_OK)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(fmt)
    log.addHandler(h)
    if to_file:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(LOG_DIR / "recorder.log", maxBytes=2_000_000, backupCount=3)
        fh.setFormatter(fmt)
        log.addHandler(fh)


# --------------------------------------------------------------------------- websocket

class WebSocketClosed(Exception):
    pass


class WebSocket:
    """Minimal RFC 6455 client: text frames, ping/pong, close."""

    def __init__(self, host: str, port: int, path: str = "/websocket", timeout: float = 8):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketClosed("handshake closed")
            head += chunk
        status, _, rest = head.partition(b"\r\n\r\n")
        if b" 101 " not in status.split(b"\r\n")[0]:
            raise WebSocketClosed("handshake refused: " + status.split(b"\r\n")[0].decode(errors="replace"))
        self.buf = rest
        self.fragments = b""

    def _send(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            hdr = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            hdr = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            hdr = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send_text(self, text: str) -> None:
        self._send(0x1, text.encode())

    def _need(self, n: int, deadline: float) -> bool:
        while len(self.buf) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return False
            if not chunk:
                raise WebSocketClosed("connection closed")
            self.buf += chunk
        return True

    def recv_text(self, timeout: float) -> Optional[str]:
        """Return the next text message, or None if none arrives in time."""
        deadline = time.monotonic() + timeout
        while True:
            if not self._need(2, deadline):
                return None
            b0, b1 = self.buf[0], self.buf[1]
            n, off = b1 & 0x7F, 2
            if n == 126:
                if not self._need(4, deadline):
                    return None
                n, off = struct.unpack("!H", self.buf[2:4])[0], 4
            elif n == 127:
                if not self._need(10, deadline):
                    return None
                n, off = struct.unpack("!Q", self.buf[2:10])[0], 10
            masked = b1 & 0x80
            total = off + (4 if masked else 0) + n
            if not self._need(total, deadline):
                return None
            payload = self.buf[off + (4 if masked else 0):total]
            if masked:
                m = self.buf[off:off + 4]
                payload = bytes(b ^ m[i % 4] for i, b in enumerate(payload))
            self.buf = self.buf[total:]
            opcode, fin = b0 & 0x0F, b0 & 0x80
            if opcode == 0x8:
                raise WebSocketClosed("closed by printer")
            if opcode == 0x9:
                self._send(0xA, payload)
                continue
            if opcode in (0x1, 0x2, 0x0):
                self.fragments += payload
                if fin:
                    text, self.fragments = self.fragments.decode(errors="replace"), b""
                    return text

    def close(self) -> None:
        try:
            self._send(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- SDCP

class SDCPError(Exception):
    pass


class Printer:
    """One SDCP connection: requests, plus the latest pushed status/attributes."""

    def __init__(self, ip: str, port: int = 3030, mainboard_id: str = ""):
        self.ip, self.port, self.mainboard_id = ip, port, mainboard_id
        self.ws: Optional[WebSocket] = None
        self.status: Dict = {}
        self.attributes: Dict = {}
        self.lock = threading.Lock()

    def connect(self) -> None:
        self.close()
        self.ws = WebSocket(self.ip, self.port)
        log.debug("connected to printer %s:%s", self.ip, self.port)

    def close(self) -> None:
        if self.ws:
            self.ws.close()
            self.ws = None

    def _handle(self, msg: Dict) -> None:
        topic = msg.get("Topic", "")
        if msg.get("MainboardID") and not self.mainboard_id:
            self.mainboard_id = msg["MainboardID"]
        if "/status/" in topic and isinstance(msg.get("Status"), dict):
            self.status = msg["Status"]
        elif "/attributes/" in topic and isinstance(msg.get("Attributes"), dict):
            self.attributes = msg["Attributes"]

    def request(self, cmd: int, data: Optional[Dict] = None, timeout: float = 6) -> Dict:
        """Send one SDCP command and return the Data of its response."""
        with self.lock:
            if not self.ws:
                self.connect()
            rid = os.urandom(8).hex()
            self.ws.send_text(json.dumps({
                "Id": os.urandom(16).hex(),
                "Data": {"Cmd": cmd, "Data": data or {}, "RequestID": rid,
                         "MainboardID": self.mainboard_id, "TimeStamp": int(time.time()), "From": 0},
                "Topic": f"sdcp/request/{self.mainboard_id}",
            }))
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise SDCPError(f"no reply to command {cmd}")
                text = self.ws.recv_text(left)
                if text is None:
                    continue
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                self._handle(msg)
                d = msg.get("Data")
                if isinstance(d, dict) and d.get("RequestID") == rid:
                    return d.get("Data") or {}

    def drain(self, seconds: float = 0.5) -> None:
        """Read any pushed messages (status/attributes) that follow a request."""
        with self.lock:
            end = time.monotonic() + seconds
            while self.ws and time.monotonic() < end:
                text = self.ws.recv_text(max(0.0, end - time.monotonic()))
                if text is None:
                    break
                try:
                    self._handle(json.loads(text))
                except ValueError:
                    pass

    def refresh_status(self) -> Dict:
        self.request(0)
        self.drain(1.0)
        return self.status

    def refresh_attributes(self) -> Dict:
        self.request(1)
        self.drain(1.0)
        return self.attributes

    def set_video(self, enable: bool) -> Dict:
        return self.request(386, {"Enable": 1 if enable else 0})


def discover(timeout: float = 3.0, port: int = 3000, target: str = "255.255.255.255") -> List[Dict]:
    """Find SDCP printers on the local network (UDP broadcast "M99999")."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.5)
    found: Dict[str, Dict] = {}
    end = time.monotonic() + timeout
    next_send = 0.0
    while time.monotonic() < end:
        if time.monotonic() >= next_send:
            try:
                s.sendto(b"M99999", (target, port))
            except OSError as e:
                log.debug("discovery send failed: %s", e)
            next_send = time.monotonic() + 1.0
        try:
            raw, addr = s.recvfrom(65536)
        except socket.timeout:
            continue
        try:
            d = json.loads(raw).get("Data", {})
        except ValueError:
            continue
        ip = d.get("MainboardIP") or addr[0]
        found[ip] = {"ip": ip, "mainboard_id": d.get("MainboardID", ""),
                     "name": d.get("Name", ""), "machine": d.get("MachineName", ""),
                     "firmware": d.get("FirmwareVersion", "")}
    s.close()
    return list(found.values())


# --------------------------------------------------------------------------- print state

def print_state(status: Dict) -> Dict:
    """Reduce an SDCP status to what the recorder needs."""
    info = status.get("PrintInfo") or {}
    machine = status.get("CurrentStatus") or []
    if isinstance(machine, int):
        machine = [machine]
    pstatus = info.get("Status", PRINT_IDLE)
    printing = MACHINE_PRINTING in machine and pstatus not in PRINT_ENDED
    return {
        "printing": printing,
        "task_id": info.get("TaskId") or info.get("Filename") or "",
        "filename": info.get("Filename") or "",
        "layer": int(info.get("CurrentLayer") or 0),
        "total_layers": int(info.get("TotalLayer") or 0),
        "print_status": pstatus,
        "error": int(info.get("ErrorNumber") or 0),
    }


def safe_name(text: str) -> str:
    text = re.sub(r"\.(goo|ctb|cbddlp|prz|jxs)$", "", text, flags=re.I)
    text = re.sub(r"[^\w\-. ]+", "_", text).strip(" ._")
    return text[:80] or "print"


# --------------------------------------------------------------------------- ffmpeg

CAFFEINATE = "/usr/bin/caffeinate"


def ffprobe_for(ffmpeg: str) -> str:
    p = str(Path(ffmpeg).with_name("ffprobe"))
    return p if os.access(p, os.X_OK) else (shutil.which("ffprobe") or "ffprobe")


def stop_gracefully(proc: subprocess.Popen, what: str = "ffmpeg") -> None:
    """Stop ffmpeg so it sends RTSP TEARDOWN and the printer frees the camera slot.

    The printer never frees a slot on its own, so a hard kill costs a slot until
    the printer is power-cycled. ffmpeg aborts the TEARDOWN if it gets a second
    signal, so: 'q', wait; one SIGINT, wait; kill only as a last resort.
    """
    if proc.poll() is not None:
        return
    try:
        proc.stdin.write(b"q")
        proc.stdin.flush()
    except (AttributeError, OSError, ValueError):
        pass
    for sig, wait in ((None, 20), (signal.SIGINT, 30)):
        if sig is not None and proc.poll() is None:
            proc.send_signal(sig)
        try:
            proc.wait(wait)
            return
        except subprocess.TimeoutExpired:
            continue
    log.error("%s did not exit; killing it (this may use up a camera slot until the printer "
              "is restarted)", what)
    proc.kill()
    proc.wait()


class Segment:
    """One ffmpeg process saving the camera stream to a .ts file."""

    def __init__(self, ffmpeg: str, url: str, path: Path, pidfile: Optional[Path] = None):
        self.path, self.pidfile = path, pidfile
        args = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "warning"]
        if url.startswith("rtsp://"):
            # Elegoo's camera only streams RTP over UDP; TCP gives "Nonmatching transport".
            args += ["-rtsp_transport", "udp", "-buffer_size", "8388608"]
        # Give up after 10 s without data, so a dead feed ends ffmpeg by itself
        # (cleanly, with TEARDOWN) instead of hanging.
        args += ["-timeout", "10000000", "-use_wallclock_as_timestamps", "1", "-fflags", "+genpts",
                 "-i", url, "-map", "0:v", "-c", "copy", "-f", "mpegts", str(path)]
        self.errlog = open(path.with_suffix(".log"), "ab")
        try:
            self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=self.errlog, start_new_session=True)
        except Exception:
            self.errlog.close()
            raise
        if pidfile:
            try:
                pidfile.write_text(str(self.proc.pid))
            except OSError:
                pass
        self.caffeinate = None
        try:  # keep the Mac awake while ffmpeg runs
            self.caffeinate = subprocess.Popen([CAFFEINATE, "-i", "-w", str(self.proc.pid)],
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
        self.last_size, self.last_growth = 0, time.monotonic()
        log.info("recording segment %s (ffmpeg pid %s)", path.name, self.proc.pid)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stalled(self, seconds: float) -> bool:
        size = self.size()
        now = time.monotonic()
        if size > self.last_size:
            self.last_size, self.last_growth = size, now
        # ffmpeg writes nothing until it has analysed the stream, so allow a slower start.
        limit = seconds if self.last_size else max(seconds, 30)
        return now - self.last_growth > limit

    def stop(self) -> None:
        stop_gracefully(self.proc)
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        self.errlog.close()
        if self.caffeinate and self.caffeinate.poll() is None:
            self.caffeinate.terminate()
        if self.pidfile:
            try:
                self.pidfile.unlink()
            except OSError:
                pass
        log.info("segment %s closed (%.1f MB)", self.path.name, self.size() / 1e6)

    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


def probe_camera(ffmpeg: str, url: str, seconds: int = 4) -> Optional[str]:
    """Read a few seconds of the live camera; return 'WIDTHxHEIGHT codec' or None.

    Uses ffmpeg (not ffprobe) because ffmpeg can be stopped gracefully, which
    sends TEARDOWN and gives the camera slot back.
    """
    args = [ffmpeg, "-hide_banner", "-nostats"]
    if url.startswith("rtsp://"):
        args += ["-rtsp_transport", "udp"]
    args += ["-timeout", "10000000", "-i", url, "-t", str(seconds), "-map", "0:v", "-c", "copy", "-f", "null", "-"]
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            start_new_session=True)
    err = b""
    try:
        _, err = proc.communicate(timeout=seconds + 25)
    except subprocess.TimeoutExpired:
        pass
    finally:
        if proc.poll() is None:
            stop_gracefully(proc)
            err += proc.stderr.read() if proc.stderr else b""
    m = re.search(r"Video: (\w+).*?, (\d{2,5})x(\d{2,5})", err.decode(errors="replace"))
    return f"{m.group(2)}x{m.group(3)} {m.group(1)}" if m else None


def measure(ffmpeg: str, parts: List[Path]) -> Tuple[int, float]:
    """Total video frames and real (wall-clock) seconds across recorded segments."""
    frames, seconds = 0, 0.0
    for p in parts:
        r = subprocess.run([ffprobe_for(ffmpeg), "-v", "error", "-select_streams", "v:0", "-count_packets",
                            "-show_entries", "stream=nb_read_packets,has_b_frames:format=duration",
                            "-of", "json", str(p)], capture_output=True, text=True)
        try:
            info = json.loads(r.stdout)
            if int(info["streams"][0].get("has_b_frames") or 0):
                return 0, 0.0  # reordered frames: leave timing alone (the Elegoo camera has none)
            frames += int(info["streams"][0]["nb_read_packets"])
            seconds += float(info["format"]["duration"])
        except (ValueError, KeyError, IndexError):
            return 0, 0.0
    return frames, seconds


def unique_path(path: Path) -> Path:
    n = 2
    candidate = path
    while candidate.exists() or candidate.is_symlink():
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return candidate


def move_into(src: Path, dest: Path, cfg: Optional[Dict]) -> Path:
    """Move a finished file into the recordings folder under a name nobody has taken."""
    dest = unique_path(dest)
    try:
        os.replace(src, dest)
    except OSError:  # different disk
        shutil.move(str(src), str(dest))
    if cfg:
        give_to_owner(cfg, dest)
    return dest


def finalize(ffmpeg: str, parts: List[Path], out: Path, keep_ts: bool, cfg: Optional[Dict] = None) -> Optional[Path]:
    """Join the .ts segments of one print into a single .mp4 (no re-encoding).

    Segments live in a private work folder; only the finished file is moved into
    the recordings folder.
    """
    parts = [p for p in parts if p.exists() and p.stat().st_size > 0]
    if not parts:
        log.warning("nothing was recorded for %s", out.name)
        return None
    work = parts[0].parent
    tmp = work / (parts[0].stem.split(".part")[0] + ".partial.mp4")
    listfile = None
    if len(parts) == 1:
        src = ["-i", str(parts[0])]
    else:
        listfile = tmp.with_suffix(".concat.txt")
        listfile.write_text("".join("file '%s'\n" % str(p).replace("'", "'\\''") for p in parts))
        src = ["-f", "concat", "-safe", "0", "-i", str(listfile)]
    # The Elegoo camera puts empty NAL units in its stream. .ts players shrug them
    # off, but in .mp4 they turn into zero-length units that break decoding.
    bsf = "filter_units=remove_types=0"
    # The camera sends evenly spaced frames, but its own clock runs ~10% fast and
    # arrival times are bursty. Space the frames evenly over the real elapsed time.
    frames, seconds = measure(ffmpeg, parts)
    if frames > 1 and seconds > 0:
        bsf += f",setts=ts=N*{seconds / frames:.9f}/TB"
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *src,
           "-map", "0:v", "-c", "copy", "-bsf:v", bsf,
           "-video_track_timescale", "90000", "-movflags", "+faststart", "-f", "mp4", str(tmp)]
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if listfile:
        listfile.unlink(missing_ok=True)
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        log.error("could not make %s (%s); saving the raw recording instead", out.name,
                  r.stderr.decode(errors="replace")[-300:].strip())
        for i, p in enumerate(parts, 1):
            move_into(p, out.with_name(f"{out.stem} raw {i}.ts"), cfg)
        return None
    final = move_into(tmp, out, cfg)
    log.info("saved %s (%.1f MB)", final, final.stat().st_size / 1e6)
    for i, p in enumerate(parts, 1):
        if keep_ts:
            move_into(p, out.with_name(f"{out.stem} raw {i}.ts"), cfg)
        else:
            p.unlink(missing_ok=True)
        p.with_suffix(".log").unlink(missing_ok=True)
    return final


# --------------------------------------------------------------------------- recorder

def pid_is_ffmpeg(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    r = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True)
    return "ffmpeg" in r.stdout


class Recorder:
    def __init__(self, cfg: Dict, config_path: Optional[Path] = None):
        self.cfg = cfg
        self.config_path = config_path
        self.ffmpeg = find_ffmpeg(cfg)
        self.out_dir = Path(cfg["output_dir"])
        self.work = work_dir(cfg)
        self.pidfile = self.work / "ffmpeg.pid"
        self.statefile = self.work / "session.json"
        self.printer = Printer(cfg["printer_ip"], int(cfg["ws_port"]), cfg.get("mainboard_id", ""))
        self.stop_event = threading.Event()
        # current print session
        self.task_id = ""
        self.base: Optional[Path] = None
        self.session = ""
        self.parts: List[Path] = []
        self.segment: Optional[Segment] = None
        self.video_on = False
        self.end_deadline: Optional[float] = None
        self.next_stream_try = 0.0
        self.last_full_warning = 0.0
        self.last_ok = time.monotonic()
        self.last_discovery = 0.0
        self.finalizers: List[threading.Thread] = []

    # ---- printer connection

    def poll(self) -> Optional[Dict]:
        try:
            st = self.printer.refresh_status()
            self.last_ok = time.monotonic()
            if self.printer.mainboard_id and self.printer.mainboard_id != self.cfg.get("mainboard_id"):
                self.cfg["mainboard_id"] = self.printer.mainboard_id
            return print_state(st)
        except (OSError, SDCPError, WebSocketClosed) as e:
            log.debug("printer poll failed: %s", e)
            self.printer.close()
            self.maybe_rediscover()
            return None

    def maybe_rediscover(self) -> None:
        """If the printer moved to a new IP (DHCP), find it again by its board ID."""
        now = time.monotonic()
        if now - self.last_ok < 60 or now - self.last_discovery < 60 or not self.cfg.get("mainboard_id"):
            return
        self.last_discovery = now
        for p in discover(port=int(self.cfg["discovery_port"])):
            if p["mainboard_id"] == self.cfg["mainboard_id"] and p["ip"] != self.printer.ip:
                log.info("printer moved from %s to %s", self.printer.ip, p["ip"])
                self.printer.ip = self.cfg["printer_ip"] = p["ip"]
                if self.config_path:
                    save_config(self.config_path, self.cfg)

    # ---- camera

    def open_stream(self) -> Optional[str]:
        """Ask the printer for the video stream, only if a camera slot is free."""
        try:
            attrs = self.printer.refresh_attributes()
            used = attrs.get("NumberOfVideoStreamConnected")
            cap = attrs.get("MaximumVideoStreamAllowed")
            if isinstance(used, int) and isinstance(cap, int) and used >= cap:
                self.warn_full(used, cap)
                return None
            self.video_on = True  # even if the reply is lost, turn it off again later
            r = self.printer.set_video(True)
        except (OSError, SDCPError, WebSocketClosed) as e:
            log.warning("could not turn the camera on: %s", e)
            self.printer.close()
            return None
        ack = r.get("Ack")
        if ack != 0:
            self.video_on = False
            if ack == 1:
                self.warn_full(None, None)
            else:
                log.warning("printer refused the camera (Ack %s: %s)", ack,
                            {2: "no camera", 3: "unknown error"}.get(ack, "?"))
            return None
        url = r.get("VideoUrl") or r.get("VideoURL") or f"{self.printer.ip}:554/video"
        if "://" not in url:
            url = "rtsp://" + url
        return url

    def warn_full(self, used, cap) -> None:
        if time.monotonic() - self.last_full_warning > 600:
            self.last_full_warning = time.monotonic()
            log.warning("camera is full (%s of %s viewers). Close other camera viewers (Lychee, the "
                        "Elegoo app). If it stays full, switch the printer off and on when it is idle: "
                        "the printer never frees stuck viewer slots on its own.", used, cap)

    def close_stream(self) -> None:
        if not self.video_on:
            return
        try:
            r = self.printer.set_video(False)
            log.info("camera turned off (Ack %s)", r.get("Ack"))
        except (OSError, SDCPError, WebSocketClosed) as e:
            log.debug("camera off request failed: %s", e)
            self.printer.close()
        self.video_on = False

    def start_segment(self) -> None:
        if time.monotonic() < self.next_stream_try:
            return
        self.next_stream_try = time.monotonic() + 60  # backoff unless this works
        url = self.open_stream()
        if not url:
            return
        path = self.work / f"{self.session}.part{len(self.parts) + 1:02d}.ts"
        try:
            self.segment = Segment(self.ffmpeg, url, path, self.pidfile)
        except Exception as e:
            log.error("could not start ffmpeg (%s): %s", self.ffmpeg, e)
            self.close_stream()
            try:
                self.ffmpeg = find_ffmpeg(dict(self.cfg, ffmpeg=""))  # e.g. after `brew upgrade`
            except SystemExit:
                pass
            return
        self.parts.append(path)
        self.save_state()
        self.next_stream_try = time.monotonic() + 10

    def stop_segment(self) -> None:
        if self.segment:
            self.segment.stop()
            self.segment = None

    def save_state(self) -> None:
        try:
            self.statefile.write_text(json.dumps({"base": str(self.base), "parts": [str(p) for p in self.parts]}))
        except OSError as e:
            log.debug("could not save state: %s", e)

    # ---- print sessions

    def begin(self, st: Dict) -> None:
        make_output_dir(self.cfg)
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H.%M")
        self.base = self.out_dir / f"{stamp} {safe_name(st['filename'])}.mp4"
        self.session = dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + os.urandom(3).hex()
        self.task_id, self.parts, self.end_deadline = st["task_id"], [], None
        log.info("print started: %s (layer %s of %s)", st["filename"], st["layer"], st["total_layers"])
        self.next_stream_try = 0.0
        self.start_segment()

    def end(self, reason: str) -> None:
        log.info("stopping recording: %s", reason)
        self.stop_segment()
        self.close_stream()
        parts, base = list(self.parts), self.base
        self.task_id, self.parts, self.base, self.end_deadline = "", [], None, None
        if base:
            self.finalize_later(parts, base)

    def finalize_later(self, parts: List[Path], base: Path) -> None:
        def job():
            try:
                finalize(self.ffmpeg, parts, base, bool(self.cfg["keep_ts"]), self.cfg)
            except Exception:
                log.exception("could not finish %s", base.name)
            finally:
                try:
                    st = json.loads(self.statefile.read_text())
                    if st.get("base") == str(base):
                        self.statefile.unlink()
                except (OSError, ValueError):
                    pass
        self.finalizers = [t for t in self.finalizers if t.is_alive()]
        t = threading.Thread(target=job)
        t.start()
        self.finalizers.append(t)

    def recover(self) -> None:
        """Clean up after a crash or forced stop: free the camera, finish old files."""
        try:
            pid = int(self.pidfile.read_text())
        except (OSError, ValueError):
            pid = 0
        if pid and pid_is_ffmpeg(pid):
            log.warning("stopping ffmpeg %s left over from before", pid)
            os.kill(pid, signal.SIGINT)  # no stdin to send 'q' to; one SIGINT keeps TEARDOWN
            for _ in range(60):
                if not pid_is_ffmpeg(pid):
                    break
                time.sleep(0.5)
            else:
                os.kill(pid, signal.SIGKILL)
            self.video_on = True
            self.close_stream()
        self.pidfile.unlink(missing_ok=True)
        try:
            st = json.loads(self.statefile.read_text())
        except (OSError, ValueError):
            return
        parts = [Path(p) for p in st.get("parts", [])]
        if st.get("base") and parts:
            log.info("finishing a recording that was interrupted: %s", Path(st["base"]).name)
            self.finalize_later(parts, Path(st["base"]))
        else:
            self.statefile.unlink(missing_ok=True)

    def step(self) -> None:
        st = self.poll()
        now = time.monotonic()
        recording = self.base is not None

        if st is None:
            if recording and now - self.last_ok > self.cfg["offline_end_seconds"] and self.end_deadline is None:
                log.info("printer unreachable; treating the print as over")
                self.end_deadline = now
        elif st["printing"]:
            if recording and st["task_id"] != self.task_id:
                self.end("a new print started")
                recording = False
            if not recording and st["layer"] >= int(self.cfg["start_at_layer"]):
                self.begin(st)
                recording = True
            elif recording and self.end_deadline is not None:
                log.info("print is running again; still recording")
                self.end_deadline = None
        elif recording and self.end_deadline is None:
            why = {PRINT_COMPLETE: "finished", PRINT_STOPPED: "was stopped"}.get(st["print_status"], "ended")
            log.info("print %s; recording %s more seconds", why, self.cfg["stop_delay_seconds"])
            self.end_deadline = now + float(self.cfg["stop_delay_seconds"])

        if self.base is None:
            return
        if self.end_deadline is not None and now >= self.end_deadline:
            self.end("stop delay over")
            return
        if self.segment and (not self.segment.alive() or self.segment.stalled(self.cfg["stall_seconds"])):
            what = "stopped" if not self.segment.alive() else "stalled"
            if self.end_deadline is not None:
                self.end(f"camera stream {what} after the print ended")
                return
            log.warning("camera stream %s; restarting it", what)
            self.stop_segment()
            self.close_stream()
        if self.segment is None:
            self.start_segment()

    def run(self) -> None:
        self.work.mkdir(parents=True, exist_ok=True)
        log.info("%s %s watching printer %s, saving to %s", APP, VERSION, self.printer.ip, self.out_dir)
        if firewall_blocks(self.ffmpeg):
            log.warning("the macOS firewall may block video for %s. Run `make install` again "
                        "(needed after `brew upgrade ffmpeg`).", os.path.realpath(self.ffmpeg))
        self.recover()
        while not self.stop_event.is_set():
            try:
                self.step()
            except Exception:  # never let one bad poll kill the service
                log.exception("unexpected error")
                self.printer.close()
            self.stop_event.wait(float(self.cfg["poll_seconds"]))
        if self.base is not None:
            self.end("recorder is shutting down")
        for t in self.finalizers:
            t.join()
        self.printer.close()
        log.info("stopped")


# --------------------------------------------------------------------------- commands

OK, BAD, NOTE = "✓", "✗", "→"


def check_ip(text: str) -> Optional[str]:
    """Return an error message if text is not a usable local printer address."""
    import ipaddress
    try:
        ip = ipaddress.ip_address(text.strip())
    except ValueError:
        return "that is not an IP address (it should look like 192.168.1.50)"
    if ip.version != 4:
        return "use the printer's IPv4 address (like 192.168.1.50)"
    if ip.is_loopback or ip.is_unspecified or ip.is_multicast or str(ip).endswith(".255"):
        return "that address can't be a printer"
    if not ip.is_private:
        return "that is not a local network address (printers use 192.168.x.x, 10.x.x.x or 172.16-31.x.x)"
    return None


def probe_printer(cfg: Dict, ip: str) -> Dict:
    """Connect to a printer and return its attributes (read-only)."""
    p = Printer(ip, int(cfg["ws_port"]))
    try:
        p.connect()
        attrs = p.refresh_attributes()
        if not attrs:
            raise SDCPError("it answered, but not like an Elegoo printer")
        attrs["_mainboard_id"] = p.mainboard_id
        return attrs
    finally:
        p.close()


def cmd_setup(cfg: Dict, args) -> None:
    """Interactive: ask for the printer's IP, check it, save settings."""
    find_ffmpeg(cfg)
    print("Looking for printers on your network...")
    found = discover(port=int(cfg["discovery_port"]))
    for p in found:
        print(f"  found {p['machine'] or 'printer'} at {p['ip']}")
    default = args.ip or (found[0]["ip"] if found else cfg.get("printer_ip", ""))
    interactive = sys.stdin.isatty() and not args.ip
    while True:
        ip = default
        if interactive:
            ip = input(f"Printer IP address{f' [{default}]' if default else ''}: ").strip() or default
        err = check_ip(ip) if ip else "please type the printer's IP address"
        if not err:
            try:
                a = probe_printer(cfg, ip)
                print(f"  {OK} {a.get('MachineName') or 'printer'} at {ip} "
                      f"(firmware {a.get('FirmwareVersion')}, camera {'yes' if a.get('CameraStatus') == 1 else 'not found'})")
                break
            except (OSError, SDCPError, WebSocketClosed) as e:
                err = f"no Elegoo printer answered at {ip} ({e}). Check it is on and on the same Wi-Fi"
        print(f"  {BAD} {err}")
        if not interactive:
            raise SystemExit(1)
    cfg["printer_ip"], cfg["mainboard_id"] = ip, a.get("_mainboard_id") or a.get("MainboardID", "")
    cfg["ffmpeg"] = find_ffmpeg(dict(cfg, ffmpeg=""))
    cfg["owner_uid"] = int(os.environ.get("SUDO_UID", os.getuid()))
    cfg["owner_gid"] = int(os.environ.get("SUDO_GID", os.getgid()))
    if args.output_dir:
        cfg["output_dir"] = os.path.expanduser(args.output_dir)
    save_config(args.write or args.config, cfg)
    print(f"  {OK} recordings will go to {cfg['output_dir']}")


def file_video_info(ffmpeg: str, path: Path) -> Optional[str]:
    """'WIDTHxHEIGHT codec' of a local recording, or None."""
    r = subprocess.run([ffprobe_for(ffmpeg), "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=codec_name,width,height", "-of", "json", str(path)], capture_output=True, text=True)
    try:
        st = json.loads(r.stdout or "{}").get("streams", [])[0]
        return f"{st['width']}x{st['height']} {st.get('codec_name', '')}".strip() if st.get("width") else None
    except (ValueError, IndexError, KeyError):
        return None


def active_segment(cfg: Dict, max_age: float = 30) -> Optional[Path]:
    now = time.time()
    dirs = [work_dir(cfg), SUPPORT_DIR / "work", Path(cfg["output_dir"]) / ".recording"]
    parts = [p for d in dirs if d.exists() for p in d.glob("*.part*.ts") if now - p.stat().st_mtime < max_age]
    return max(parts, key=lambda p: p.stat().st_mtime) if parts else None


def cmd_check(cfg: Dict, args) -> None:
    """Check the camera feed end to end."""
    if not cfg["printer_ip"]:
        raise SystemExit("No printer set up yet. Run: make install")
    ff = find_ffmpeg(cfg)
    p = Printer(cfg["printer_ip"], int(cfg["ws_port"]), cfg.get("mainboard_id", ""))
    try:
        st = print_state(p.refresh_status())
        a = p.refresh_attributes()
    except (OSError, SDCPError, WebSocketClosed) as e:
        raise SystemExit(f"  {BAD} printer not reachable at {cfg['printer_ip']}: {e}")
    finally:
        p.close()
    print(f"  {OK} printer {a.get('MachineName')} at {cfg['printer_ip']}")
    used, cap = a.get("NumberOfVideoStreamConnected"), a.get("MaximumVideoStreamAllowed")

    if st["printing"]:
        print(f"  {OK} printing {st['filename']} (layer {st['layer']} of {st['total_layers']})")
        if st["layer"] < int(cfg["start_at_layer"]):
            print(f"  {NOTE} recording starts at layer {cfg['start_at_layer']}; waiting...")
        seg, end = None, time.monotonic() + 150
        while time.monotonic() < end and not seg:
            seg = active_segment(cfg)
            if not seg:
                time.sleep(3)
        if not seg:
            if isinstance(used, int) and isinstance(cap, int) and used >= cap:
                why = (f"the camera is full ({used} of {cap} viewers). Close Lychee's camera view or the Elegoo "
                       "app, or restart the printer once it is idle")
            else:
                why = "the service is not recording. Look at: make logs"
            raise SystemExit(f"  {BAD} no recording yet: {why}")
        time.sleep(4)
        info = file_video_info(ff, seg)
        if not info:
            raise SystemExit(f"  {BAD} the recording has no readable video yet. Look at: make logs")
        print(f"  {OK} recording now: {info} -> {cfg['output_dir']}")
        return

    print(f"  {NOTE} no print running")
    if isinstance(used, int) and isinstance(cap, int) and used >= cap:
        raise SystemExit(f"  {BAD} the camera is full ({used} of {cap} viewers). Restart the printer "
                         "(it never frees stuck viewers on its own), then run: make check")
    rec = Recorder(cfg)
    info = None
    try:
        url = rec.open_stream()
        info = probe_camera(ff, url) if url else None
    finally:  # also on Ctrl-C: always hand the camera back
        rec.close_stream()
        rec.printer.close()
    if info:
        print(f"  {OK} camera feed works: {info}")
    else:
        print(f"  {NOTE} the camera only sends video while a print is running.\n"
              f"    Start a print, then run: make check")


def cmd_discover(cfg: Dict, args) -> None:
    found = discover(port=int(cfg["discovery_port"]))
    print(json.dumps(found, indent=2) if found else "No printers found.")


def cmd_status(cfg: Dict, args) -> None:
    """Read-only: print status and camera slots. Never turns the camera on."""
    if not cfg["printer_ip"]:
        raise SystemExit("No printer set up yet. Run: make install")
    p = Printer(cfg["printer_ip"], int(cfg["ws_port"]), cfg.get("mainboard_id", ""))
    st = print_state(p.refresh_status())
    a = p.refresh_attributes()
    p.close()
    seg = active_segment(cfg)
    print(json.dumps({
        "printer": a.get("MachineName"), "firmware": a.get("FirmwareVersion"), "ip": cfg["printer_ip"],
        "printing": st["printing"], "file": st["filename"],
        "layer": f"{st['layer']}/{st['total_layers']}", "print_status": st["print_status"],
        "camera_viewers": f"{a.get('NumberOfVideoStreamConnected')}/{a.get('MaximumVideoStreamAllowed')}",
        "recording": seg.name if seg else None,
        "recordings_folder": cfg["output_dir"],
    }, indent=2))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog=APP, description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", help="watch the printer and record every print (what the service runs)")
    s = sub.add_parser("setup", help="ask for the printer's IP, check it, save settings")
    s.add_argument("--ip")
    s.add_argument("--output-dir")
    s.add_argument("--write", type=Path, help="write settings here instead of --config")
    sub.add_parser("check", help="check the camera feed")
    sub.add_parser("status", help="show print status and camera viewers (read-only)")
    sub.add_parser("discover", help="list printers on this network")
    sub.add_parser("version")
    args = ap.parse_args(argv)
    cmd = args.cmd or "run"
    setup_logging(args.verbose, to_file=(cmd == "run"))
    cfg = load_config(args.config)

    if cmd == "version":
        print(VERSION)
        return
    if cmd != "run":
        {"setup": cmd_setup, "check": cmd_check, "status": cmd_status, "discover": cmd_discover}[cmd](cfg, args)
        return
    if not cfg["printer_ip"]:
        raise SystemExit("No printer set up yet. Run: make install")
    rec = Recorder(cfg, args.config if os.access(args.config.parent, os.W_OK) else None)

    def on_signal(signum, _frame):
        log.info("got signal %s, finishing up", signum)
        rec.stop_event.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    rec.run()


if __name__ == "__main__":
    main()

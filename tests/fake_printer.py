"""A pretend Elegoo printer for tests: SDCP WebSocket + a live test video feed."""

import base64
import hashlib
import json
import socket
import struct
import subprocess
import threading


class FakePrinter:
    def __init__(self, ffmpeg="ffmpeg"):
        self.ffmpeg = ffmpeg
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.bind(("127.0.0.1", 0))
        self.video_port = u.getsockname()[1]
        u.close()
        self.lock = threading.Lock()
        self.machine, self.print_status, self.layer = [0], 0, 0
        self.filename, self.task = "", ""
        self.viewers, self.max_viewers = 0, 2
        self.calls = []          # (cmd, data) for every request
        self.feeder = None
        self.running = True
        threading.Thread(target=self._accept, daemon=True).start()

    # ---- test controls
    def start_print(self, filename="Test Part.goo", task="task-1"):
        with self.lock:
            self.machine, self.print_status, self.layer = [1], 2, 1
            self.filename, self.task = filename, task

    def next_layer(self):
        with self.lock:
            self.layer += 1

    def finish_print(self):
        with self.lock:
            self.machine, self.print_status = [0], 9

    def drop_video(self):
        self._stop_feeder()

    def video_calls(self):
        return [d.get("Enable") for c, d in self.calls if c == 386]

    def close(self):
        self.running = False
        self._stop_feeder()
        self.srv.close()

    # ---- video
    def _start_feeder(self):
        if self.feeder and self.feeder.poll() is None:
            return
        self.feeder = subprocess.Popen(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi",
             "-i", "testsrc=size=320x240:rate=15", "-c:v", "libx264", "-preset", "ultrafast",
             "-tune", "zerolatency", "-g", "15", "-f", "mpegts",
             f"udp://127.0.0.1:{self.video_port}?pkt_size=1316"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _stop_feeder(self):
        if self.feeder and self.feeder.poll() is None:
            self.feeder.terminate()
            self.feeder.wait(5)
        self.feeder = None

    # ---- websocket server
    def _accept(self):
        while self.running:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(c,), daemon=True).start()

    def _send(self, c, obj):
        p = json.dumps(obj).encode()
        n = len(p)
        hdr = struct.pack("!BB", 0x81, n) if n < 126 else struct.pack("!BBH", 0x81, 126, n)
        c.sendall(hdr + p)

    def _client(self, c):
        try:
            req = b""
            while b"\r\n\r\n" not in req:
                req += c.recv(4096)
            key = [l.split(b":", 1)[1].strip() for l in req.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
            acc = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
            buf = b""
            while self.running:
                chunk = c.recv(65536)
                if not chunk:
                    return
                buf += chunk
                while len(buf) >= 2:
                    op, n, off = buf[0] & 15, buf[1] & 127, 2
                    if n == 126:
                        n, off = struct.unpack("!H", buf[2:4])[0], 4
                    if len(buf) < off + 4 + n:
                        break
                    m = buf[off:off + 4]
                    payload = bytes(b ^ m[i % 4] for i, b in enumerate(buf[off + 4:off + 4 + n]))
                    buf = buf[off + 4 + n:]
                    if op == 8:
                        return
                    if op == 1:
                        self._handle(c, json.loads(payload))
        except OSError:
            pass
        finally:
            c.close()

    def _handle(self, c, msg):
        d = msg["Data"]
        cmd, data = d["Cmd"], d.get("Data") or {}
        self.calls.append((cmd, data))
        reply = {"Ack": 0}
        if cmd == 386:
            if data.get("Enable"):
                if self.viewers >= self.max_viewers:
                    reply = {"Ack": 1}
                else:
                    self.viewers += 1
                    self._start_feeder()
                    reply = {"Ack": 0, "VideoUrl": f"udp://127.0.0.1:{self.video_port}"}
            else:
                self.viewers = max(0, self.viewers - 1)
                self._stop_feeder()
        self._send(c, {"Id": "x", "Data": {"Cmd": cmd, "Data": reply, "RequestID": d["RequestID"],
                                            "MainboardID": "fakeboard"}, "Topic": "sdcp/response/fakeboard"})
        with self.lock:
            if cmd == 0:
                self._send(c, {"Status": {"CurrentStatus": self.machine, "PrintInfo": {
                    "Status": self.print_status, "CurrentLayer": self.layer, "TotalLayer": 100,
                    "Filename": self.filename, "TaskId": self.task, "ErrorNumber": 0}},
                    "MainboardID": "fakeboard", "Topic": "sdcp/status/fakeboard"})
            elif cmd == 1:
                self._send(c, {"Attributes": {"MachineName": "Fake Saturn", "FirmwareVersion": "V0",
                                              "NumberOfVideoStreamConnected": self.viewers,
                                              "MaximumVideoStreamAllowed": self.max_viewers,
                                              "CameraStatus": 1, "MainboardID": "fakeboard"},
                               "MainboardID": "fakeboard", "Topic": "sdcp/attributes/fakeboard"})

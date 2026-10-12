"""
host.py — the shared machine side of the P2P remote desktop.

Run this on the PC you want to control:

    python host.py              # listen on the configured port (default 47800)
    python host.py --port 5000  # custom port
    python host.py --no-gui     # headless server (for testing / services)

The host captures the screen with mss, encodes JPEG frames, and serves ONE
client at a time over a single TCP connection:

    client --> host : AUTH, MOUSE, KEY, CHAT, QUALITY, MONITOR, FILE_*, PING
    host --> client : AUTH_RESULT, HELLO, FRAME, CHAT, CLIPBOARD, PONG, BYE

Frames, chat, and file chunks are interleaved on the same socket; the client
is responsible for demultiplexing. Every inbound client message is queued and
drained on the Tk event loop so the GUI never blocks on the wire.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import queue
import socket
import socketserver
import threading
import time
import tkinter as tk
from tkinter import ttk

import mss
from PIL import Image

import protocol as P
import uikit as U
from uikit import (ACCENT, AMBER, BG, GREEN, MUTED, PANEL, PANEL2, RED, FG)
import wininput as W

log = logging.getLogger("host")


# ------------------------------------------------------------- capture worker
class CaptureEngine:
    """Grabs the requested monitor, scales/encodes JPEG, tracks bandwidth."""

    def __init__(self):
        self.sct = mss.mss()
        self.quality = 70
        self.fps = 30
        self.scale_pct = 100
        self.monitor_index = 1          # 1-based index into mss monitors
        self.frame_interval = 1.0 / 30.0
        self._last_shot = 0.0
        self.last_jpeg = None
        self.last_capture_at = 0.0
        self.bytes_sent = 0
        self.frames_sent = 0
        self.skipped = 0

    def configure(self, quality=None, fps=None, scale_pct=None, monitor_index=None):
        if quality is not None:
            self.quality = max(10, min(95, int(quality)))
        if scale_pct is not None:
            self.scale_pct = max(25, min(100, int(scale_pct)))
        if monitor_index is not None:
            self.monitor_index = int(monitor_index)
        if fps is not None:
            self.fps = max(1, min(60, int(fps)))
            self.frame_interval = 1.0 / self.fps

    def monitors(self):
        """List of dicts: {"index": i, "name": ..., "w":, "h":} (0 = all)."""
        out = []
        for i, m in enumerate(self.sct.monitors):
            if i == 0:
                out.append({"index": 0, "name": "All monitors (combined)",
                            "w": m["width"], "h": m["height"]})
            else:
                out.append({"index": i, "name": f"Display {i}",
                            "w": m["width"], "h": m["height"]})
        return out

    def current_size(self):
        mons = self.sct.monitors
        idx = self.monitor_index if 0 <= self.monitor_index < len(mons) else 0
        m = mons[idx]
        return m["width"], m["height"]

    def grab(self):
        """Capture + encode if enough time has passed since the last frame.

        Returns (jpeg_bytes or None, monitor_dict). Frames are rate-limited by
        frame_interval so a slow client simply accumulates 'skipped' frames
        instead of queueing up (backpressure by dropping, like every real
        remote-desktop app).
        """
        now = time.monotonic()
        mons = self.sct.monitors
        idx = self.monitor_index if 0 <= self.monitor_index < len(mons) else 0
        m = mons[idx]

        shot = self.sct.grab(m)
        raw = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        if self.scale_pct < 100:
            w = max(1, int(raw.width * self.scale_pct / 100))
            h = max(1, int(raw.height * self.scale_pct / 100))
            raw = raw.resize((w, h), Image.BILINEAR)

        buf = io.BytesIO()
        raw.save(buf, format="JPEG", quality=self.quality, optimize=False)
        jpeg = buf.getvalue()
        self.last_jpeg = jpeg
        self.last_capture_at = time.time()

        if now - self._last_shot < self.frame_interval * 0.75:
            self.skipped += 1
            return None, m
        self._last_shot = now
        self.frames_sent += 1
        self.bytes_sent += len(jpeg)
        return jpeg, m


# ------------------------------------------------------------ inbound handler
class InboundHandler:
    """Serializes client->host events and applies them to Windows."""

    def __init__(self, engine: CaptureEngine, chat=None, session=None):
        self.engine = engine
        self.chat = chat
        self.session = session          # set for live input
        self.allow_control = True
        self.allow_clipboard = True
        self.last_clipboard_pull = 0.0
        self.remote_clipboard = ""
        self.quality_dirty = False

    # ---- mouse -----------------------------------------------------------
    def on_mouse(self, msg: dict) -> None:
        if not self.allow_control or not self.session:
            return
        kind = msg.get("type")
        try:
            if kind == "move":
                W.move_mouse(int(msg["x"]), int(msg["y"]))
            elif kind == "down":
                W.mouse_button(msg.get("button", "left"), True)
            elif kind == "up":
                W.mouse_button(msg.get("button", "left"), False)
            elif kind == "wheel":
                W.wheel(int(msg.get("delta", -120)))
        except Exception as exc:  # noqa: BLE001 - input injection is best effort
            log.warning("mouse inject failed: %s", exc)

    # ---- keyboard --------------------------------------------------------
    def on_key(self, msg: dict) -> None:
        if not self.allow_control or not self.session:
            return
        try:
            up = msg.get("type") == "up"
            uni = msg.get("uni") or ""
            if uni and ord(uni[0]) >= 32:
                W.key_unicode(uni[0], up=up)
            else:
                W.key_vk(int(msg.get("vk", 0)), up=up)
        except Exception as exc:  # noqa: BLE001
            log.warning("key inject failed: %s", exc)

    # ---- clipboard -------------------------------------------------------
    def on_clipboard(self, msg: dict) -> None:
        text = msg.get("text", "")
        self.remote_clipboard = text
        if self.allow_clipboard and text:
            W.set_clipboard_text(text)

    def poll_clipboard(self, send_fn) -> None:
        """Push host clipboard changes to the client (~4 Hz while connected)."""
        now = time.monotonic()
        if now - self.last_clipboard_pull < 0.25:
            return
        self.last_clipboard_pull = now
        if not self.allow_clipboard or not send_fn:
            return
        text = W.get_clipboard_text()
        if text is None or text == self.remote_clipboard:
            return
        self.remote_clipboard = text
        try:
            send_fn(P.CLIPBOARD, {"text": text})
        except P.ConnectionLost:
            pass

    # ---- chat ------------------------------------------------------------
    def on_chat(self, text: str) -> None:
        if self.chat:
            self.chat.add(text, "remote")

    # ---- stream control --------------------------------------------------
    def on_quality(self, msg: dict) -> None:
        self.quality_dirty = True
        self.engine.configure(
            quality=msg.get("quality"),
            fps=msg.get("fps"),
            scale_pct=msg.get("scale"),
        )

    def on_monitor(self, msg: dict) -> None:
        self.engine.monitor_index = int(msg.get("index", 1))


# ------------------------------------------------------------------ session
class ClientSession(threading.Thread):
    """One accepted client: owns the socket, pumps frames, routes events.

    The socketserver handler thread never closes this socket — only the
    session does, at teardown — so ownership is unambiguous.
    """

    def __init__(self, sock: socket.socket, addr, engine: CaptureEngine,
                 password: str, allow_control=True, allow_files=True,
                 allow_clipboard=True, on_event=None, host_ui=None):
        super().__init__(daemon=True, name=f"session-{addr[0]}")
        self.sock = sock
        self.addr = addr
        self.engine = engine
        self.password = password or ""
        self.allow_control = allow_control
        self.allow_files = allow_files
        self.allow_clipboard = allow_clipboard
        self.on_event = on_event or (lambda kind, data=None: None)
        self.host_ui = host_ui
        self.stop_flag = threading.Event()
        self.authenticated = not self.password
        self.send_lock = threading.Lock()
        self.events: "queue.Queue[tuple[int, object]]" = queue.Queue()
        self.frames_sent = 0
        self.connected_at = time.time()
        self.inbound = InboundHandler(engine, chat=None, session=self)
        self.inbound.allow_control = allow_control
        self.inbound.allow_clipboard = allow_clipboard
        self.downloads_dir = (
            U.load_settings().get("downloads_dir")
            or os.path.join(os.path.expanduser("~"), "Downloads")
        )
        # file receive state
        self._file_id = 0
        self._recv_files: dict[str, dict] = {}

    # ---- lifecycle -------------------------------------------------------
    def run(self) -> None:
        self._loop()

    def _loop(self) -> None:
        try:
            self._loop_impl()
        except (P.ConnectionLost, P.ProtocolError, OSError) as exc:
            log.info("session %s ended: %s", self.addr[0], exc)
        finally:
            self.stop_flag.set()
            self.on_event("disconnected", {"addr": self.addr})
            self._cleanup()

    def _loop_impl(self) -> None:
        # 1. authenticate (always read AUTH — a blank password still
        #    requires the handshake, so the client gets a definitive
        #    AUTH_RESULT instead of blocking on the wire)
        mtype, payload = P.recv_msg(self.sock)
        if mtype != P.AUTH:
            self.send_json(P.AUTH_RESULT,
                           {"ok": False, "why": "expected AUTH first"})
            return
        supplied = json.loads(payload.decode("utf-8")).get("password", "")
        if supplied != self.password:
            self.send_json(P.AUTH_RESULT, {"ok": False,
                                           "why": "wrong password"})
            return
        self.authenticated = True
        self.send_json(P.AUTH_RESULT, {"ok": True, "why": ""})

        # 2. greet the client with screen info
        w, h = self.engine.current_size()
        self.send_json(P.HELLO, {
            "screen_w": w,
            "screen_h": h,
            "monitors": self.engine.monitors(),
            "hostname": socket.gethostname(),
        })
        self.send_json(P.CTRL_STATE, {
            "control": self.allow_control,
            "files": self.allow_files,
            "clipboard": self.allow_clipboard,
        })
        self.on_event("connected", {"addr": self.addr})

        # 3. pump frames + drain inbound events until stop
        reader = threading.Thread(target=self._reader, daemon=True)
        reader.start()
        next_frame = time.monotonic()
        while not self.stop_flag.is_set():
            self._drain_events()
            self.inbound.poll_clipboard(self.send)

            if self.authenticated:
                jpeg, mon = self.engine.grab()
                if jpeg is not None:
                    self.send(P.FRAME, jpeg)
                    self.frames_sent += 1

            next_frame += self.engine.frame_interval
            delay = next_frame - time.monotonic()
            if delay < 0:
                next_frame = time.monotonic()
                delay = 0.0005
            if self.stop_flag.wait(delay):
                break

    def _cleanup(self) -> None:
        self.stop_flag.set()
        for info in self._recv_files.values():
            try:
                if info.get("fh") and not info["fh"].closed:
                    info["fh"].close()
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass

    def stop(self) -> None:
        self.stop_flag.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    # ---- send helpers ----------------------------------------------------
    def send(self, mtype: int, payload=b"") -> None:
        with self.send_lock:
            P.send_msg(self.sock, mtype, payload)

    def send_json(self, mtype: int, obj) -> None:
        self.send(mtype, json.dumps(obj).encode("utf-8"))

    # ---- main loop -------------------------------------------------------
    def _loop(self) -> None:
        # 1. authenticate (always read AUTH — a blank password still
        #    requires the handshake, so the client gets a definitive
        #    AUTH_RESULT instead of blocking on the wire)
        mtype, payload = P.recv_msg(self.sock)
        if mtype != P.AUTH:
            try:
                self.send_json(P.AUTH_RESULT,
                               {"ok": False, "why": "expected AUTH first"})
            except P.ConnectionLost:
                pass
            return
        supplied = json.loads(payload.decode("utf-8")).get("password", "")
        if supplied != self.password:
            self.send_json(P.AUTH_RESULT, {"ok": False,
                                           "why": "wrong password"})
            return
        self.authenticated = True
        self.send_json(P.AUTH_RESULT, {"ok": True, "why": ""})

        # 2. greet the client with screen info
        w, h = self.engine.current_size()
        self.send_json(P.HELLO, {
            "screen_w": w,
            "screen_h": h,
            "monitors": self.engine.monitors(),
            "hostname": socket.gethostname(),
        })
        self.send_json(P.CTRL_STATE, {
            "control": self.allow_control,
            "files": self.allow_files,
            "clipboard": self.allow_clipboard,
        })
        self.on_event("connected", {"addr": self.addr})

        # 3. pump frames + drain inbound events until stop
        reader = threading.Thread(target=self._reader, daemon=True)
        reader.start()
        next_frame = time.monotonic()
        while not self.stop_flag.is_set():
            self._drain_events()
            self.inbound.poll_clipboard(self.send)

            if self.authenticated:
                jpeg, mon = self.engine.grab()
                if jpeg is not None:
                    try:
                        self.send(P.FRAME, jpeg)
                        self.frames_sent += 1
                    except P.ConnectionLost:
                        break

            next_frame += self.engine.frame_interval
            delay = next_frame - time.monotonic()
            if delay < 0:
                next_frame = time.monotonic()
                delay = 0.0005
            if self.stop_flag.wait(delay):
                break

    def _drain_events(self) -> None:
        """Apply queued client->host messages (runs on the pump thread)."""
        while not self.stop_flag.is_set():
            try:
                mtype, payload = self.events.get_nowait()
            except queue.Empty:
                return
            try:
                if mtype == P.MOUSE:
                    self.inbound.on_mouse(json.loads(payload))
                elif mtype == P.KEY:
                    self.inbound.on_key(json.loads(payload))
                elif mtype == P.CHAT:
                    self._on_chat(payload)
                elif mtype == P.QUALITY:
                    self.inbound.on_quality(json.loads(payload))
                elif mtype == P.MONITOR:
                    self.inbound.on_monitor(json.loads(payload))
                elif mtype == P.FILE_OFFER:
                    self._on_file_offer(payload)
                elif mtype == P.FILE_ACCEPT:
                    self._on_file_accept(payload)
                elif mtype == P.FILE_DATA:
                    self._on_file_data(payload)
                elif mtype == P.FILE_DONE:
                    self._on_file_done(payload)
                elif mtype == P.CLIPBOARD:
                    self.inbound.on_clipboard(json.loads(payload))
                elif mtype == P.PING:
                    self.send(P.PONG, payload)
                elif mtype == P.BYE:
                    self.stop_flag.set()
                    try:
                        self.send_json(P.CTRL_STATE, {})
                    except P.ConnectionLost:
                        pass
            except (ValueError, KeyError) as exc:
                log.warning("bad message %s: %s", mtype, exc)

    def _reader(self) -> None:
        """Dedicated thread: read frames off the socket, queue them."""
        while not self.stop_flag.is_set():
            try:
                mtype, payload = P.recv_msg(self.sock)
            except (P.ConnectionLost, P.ProtocolError) as exc:
                log.info("reader %s: %s", self.addr[0], exc)
                self.stop_flag.set()
                return
            if mtype is None:
                self.stop_flag.set()
                return
            self.events.put((mtype, payload))

    # ---- chat ------------------------------------------------------------
    def _on_chat(self, payload: bytes) -> None:
        try:
            text = json.loads(payload).get("text", "")
        except ValueError:
            return
        self.on_event("chat", {"text": text})
        if self.host_ui:
            self.host_ui.enqueue_chat(text)

    def say(self, text: str) -> None:
        self.send_json(P.CHAT, {"text": text})

    # ---- files -----------------------------------------------------------
    def _on_file_offer(self, payload: bytes) -> None:
        try:
            offer = json.loads(payload)
        except ValueError:
            return
        fid = str(offer.get("id", ""))
        name = os.path.basename(offer.get("name", "file.bin"))
        size = int(offer.get("size", 0))
        if not self.allow_files:
            self.send_json(P.FILE_ACCEPT, {
                "id": fid, "ok": False, "why": "file transfer is disabled",
            })
            return
        # host_ui is None in headless/test mode: auto-accept to the
        # downloads dir instead of blocking on a save dialog
        if self.host_ui is None:
            save_path = os.path.join(self.downloads_dir, name)
        else:
            save_path = U.pick_save_path(self.host_ui, name, self.downloads_dir)
        if not save_path:
            self.send_json(P.FILE_ACCEPT, {
                "id": fid, "ok": False, "why": "declined by host",
            })
            return
        try:
            fh = open(save_path, "wb")
        except OSError as exc:
            self.send_json(P.FILE_ACCEPT, {
                "id": fid, "ok": False, "why": f"cannot write: {exc}",
            })
            return
        self._recv_files[fid] = {
            "fh": fh, "path": save_path, "size": size, "received": 0,
            "name": name, "id": fid,
        }
        self.send_json(P.FILE_ACCEPT, {
            "id": fid, "ok": True, "save_path": save_path, "why": "",
        })
        self.on_event("file_offer", {"name": name, "size": size,
                                     "save_path": save_path})

    def _on_file_accept(self, payload: bytes) -> None:
        try:
            info = json.loads(payload)
        except ValueError:
            return
        self.on_event("file_accept", info)
        # the client offered this file and the host accepted: stream it
        pending = self._pending_send
        if not pending or info.get("id") != pending["id"]:
            return
        if not info.get("ok"):
            self.on_event("error",
                          {"text": f"Host declined: {info.get('why','')}"})
            self._pending_send = None
            return
        # send from a dedicated thread so frames/chat keep flowing
        threading.Thread(target=self._send_file_worker,
                         args=(pending,), daemon=True).start()

    def _send_file_worker(self, pending: dict) -> None:
        try:
            with open(pending["path"], "rb") as fh:
                while not self.stop_flag.is_set():
                    chunk = fh.read(CHUNK)
                    if not chunk:
                        break
                    self.send(P.FILE_DATA, chunk)
                    pending["sent"] += len(chunk)
                    self.on_event("send_progress", dict(pending))
            self.send_json(P.FILE_DONE, {"id": pending["id"], "ok": True})
        except (OSError, P.ConnectionLost) as exc:
            self.on_event("error", {"text": f"Send failed: {exc}"})
            try:
                self.send_json(P.FILE_DONE, {"id": pending["id"], "ok": False})
            except P.ConnectionLost:
                pass
        finally:
            self._pending_send = None

    def _on_file_data(self, payload: bytes) -> None:
        fid = self._pending_file_id()
        if not fid:
            return
        info = self._recv_files[fid]
        try:
            info["fh"].write(payload)
            info["received"] += len(payload)
        except OSError:
            self._abort_recv(info)
        self.on_event("file_progress", {
            "id": fid, "received": info["received"],
            "size": info["size"], "name": info["name"],
        })

    def _pending_file_id(self):
        for fid, info in self._recv_files.items():
            if not info.get("done"):
                return fid
        return ""

    def _abort_recv(self, info: dict) -> None:
        try:
            info["fh"].close()
        except OSError:
            pass
        info["failed"] = True

    def _on_file_done(self, payload: bytes) -> None:
        try:
            done = json.loads(payload)
        except ValueError:
            return
        fid = str(done.get("id", ""))
        info = self._recv_files.get(fid)
        if not info:
            return
        try:
            info["fh"].close()
        except OSError:
            pass
        info["done"] = True
        self.send_json(P.FILE_DONE, {"id": fid, "ok": True})
        self.on_event("file_done", {
            "name": info["name"], "size": info["size"],
            "path": info.get("path"),
        })


# ------------------------------------------------------------- TCP listener
class HostServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 8

    def __init__(self, addr, engine, password, allow_control, allow_files,
                 allow_clipboard, on_event=None, host_ui=None):
        self.engine = engine
        self.password = password
        self.allow_control = allow_control
        self.allow_files = allow_files
        self.allow_clipboard = allow_clipboard
        self.on_event = on_event
        self.host_ui = host_ui
        self.session: ClientSession | None = None
        self.session_lock = threading.Lock()
        super().__init__(addr, HostHandler)

    def verify_request(self, request, client_address):
        # one client at a time; refuse a second *connection* outright
        with self.session_lock:
            if self.session and self.session.is_alive():
                return False
        return True

    def process_request(self, request, client_address):
        """Accept immediately, then hand the socket to a session thread.

        finish_request is bypassed so the socketserver machinery never
        touches the socket again (see shutdown_request).
        """
        session = ClientSession(
            request, client_address, self.engine,
            password=self.password,
            allow_control=self.allow_control,
            allow_files=self.allow_files,
            allow_clipboard=self.allow_clipboard,
            on_event=self.on_event,
            host_ui=self.host_ui,
        )
        with self.session_lock:
            self.session = session
        session.start()

    def shutdown_request(self, request):
        # Do NOT close/shutdown the socket here: BaseRequestHandler would
        # shut it down while the session thread is still sending frames.
        # The session owns the socket lifecycle and closes it in _cleanup().
        pass


class HostHandler(socketserver.BaseRequestHandler):
    def handle(self):
        P.tune_socket(self.request)
        # The session was created in finish_request; this handler thread owns
        # the socket for its lifetime, so hold it here until the session ends.
        session = self.server.session
        if not session:
            return
        while session.is_alive() and not getattr(self.request, "_closed", False):
            time.sleep(0.05)


# ------------------------------------------------------------------- GUI
class HostApp:
    """Tk control room: start/stop the server, watch the session, chat."""

    def __init__(self, root: tk.Tk, cfg: dict):
        self.root = root
        self.cfg = cfg
        self.engine = CaptureEngine()
        self.server: HostServer | None = None
        self.server_thread: threading.Thread | None = None
        self.event_q: "queue.Queue[tuple[str, dict]]" = queue.Queue()
        self.chat_q: "queue.Queue[str]" = queue.Queue()
        self.stats = {"frames": 0, "bytes": 0, "clients": 0, "started": 0.0}

        U.setup_theme(root)
        U.center(root, 780, 560)
        root.title("P2P Remote Desktop — Host")
        root.minsize(700, 520)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_ui()
        self._poll_events()
        root.after(1000, self._tick_stats)
        self.log(f"Ready. Host will listen on port {self.cfg['host_port']}.")

    # ---- ui --------------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}
        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True, padx=8, pady=8)

        header = ttk.Frame(main)
        header.pack(fill="x", **pad)
        ttk.Label(header, text="P2P Remote Desktop",
                  font=("Segoe UI Semibold", 15)).pack(side="left")
        self.badge = tk.Label(header, text="● offline", bg=BG, fg=RED,
                              font=("Segoe UI", 10))
        self.badge.pack(side="right")

        top = ttk.Frame(main, style="Card.TFrame", padding=12)
        top.pack(fill="x", **pad)

        row1 = ttk.Frame(top, style="Card.TFrame")
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="Your address", style="Card.Muted.TLabel",
                  width=13).pack(side="left")
        self.addr_var = tk.StringVar(
            value=f"{U.local_ip()}:{self.cfg['host_port']}")
        entry = ttk.Entry(row1, textvariable=self.addr_var, state="readonly",
                          font=("Consolas", 11))
        entry.pack(side="left", fill="x", expand=True)
        self.copy_btn = ttk.Button(row1, text="Copy", width=7,
                                   command=self._copy_addr)
        self.copy_btn.pack(side="left", padx=(8, 0))

        row2 = ttk.Frame(top, style="Card.TFrame")
        row2.pack(fill="x", pady=(8, 2))
        ttk.Label(row2, text="Port", style="Card.Muted.TLabel", width=13).pack(
            side="left")
        self.port_var = tk.StringVar(value=str(self.cfg["host_port"]))
        ttk.Entry(row2, textvariable=self.port_var, width=10).pack(side="left")
        ttk.Label(row2, text="Password", style="Card.Muted.TLabel",
                  ).pack(side="left", padx=(18, 6))
        self.pw_var = tk.StringVar(value=self.cfg["host_password"])
        ttk.Entry(row2, textvariable=self.pw_var, width=18, show="•").pack(
            side="left")
        ttk.Label(row2, text="(blank = no auth)", style="Card.Muted.TLabel",
                  font=("Segoe UI", 8)).pack(side="left", padx=(6, 0))

        opts = ttk.Frame(top, style="Card.TFrame")
        opts.pack(fill="x", pady=(10, 2))
        self.ctrl_var = tk.BooleanVar(value=self.cfg["host_allow_control"])
        self.files_var = tk.BooleanVar(value=self.cfg["host_allow_files"])
        self.clip_var = tk.BooleanVar(value=self.cfg["host_allow_clipboard"])
        ttk.Checkbutton(opts, text="Allow remote control (mouse+keyboard)",
                        variable=self.ctrl_var, style="TCheckbutton").pack(
            side="left")
        ttk.Checkbutton(opts, text="File transfer", variable=self.files_var).pack(
            side="left", padx=(14, 0))
        ttk.Checkbutton(opts, text="Clipboard sync", variable=self.clip_var).pack(
            side="left", padx=(14, 0))

        btns = ttk.Frame(top, style="Card.TFrame")
        btns.pack(fill="x", pady=(10, 0))
        self.start_btn = ttk.Button(btns, text="Start sharing",
                                    style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Stop", style="Danger.TButton",
                                   command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))

        # monitor picker
        mon_row = ttk.Frame(top, style="Card.TFrame")
        mon_row.pack(fill="x", pady=(10, 0))
        ttk.Label(mon_row, text="Share", style="Card.Muted.TLabel").pack(
            side="left", padx=(0, 8))
        self.mon_var = tk.StringVar(value="All monitors (combined)")
        self.mon_combo = ttk.Combobox(mon_row, textvariable=self.mon_var,
                                      state="readonly", width=30)
        self.mon_combo.pack(side="left")
        self.mon_combo.bind("<<ComboboxSelected>>", self._on_monitor_pick)
        self._refresh_monitor_list()

        # --- lower half: log + chat ---------------------------------------
        lower = ttk.Frame(main)
        lower.pack(fill="both", expand=True, **pad)

        logcard = ttk.Frame(lower, style="Card.TFrame", padding=10)
        logcard.pack(side="left", fill="both", expand=True)
        ttk.Label(logcard, text="Activity", style="Card.TLabel").pack(anchor="w")
        self.log_text = tk.Text(logcard, bg=PANEL2, fg=FG, relief="flat",
                                font=("Consolas", 9), state="disabled",
                                wrap="word", padx=8, pady=6)
        self.log_text.pack(fill="both", expand=True, pady=(6, 0))
        for tag, color in (("info", FG), ("ok", GREEN), ("warn", AMBER),
                           ("err", RED)):
            self.log_text.tag_configure(tag, foreground=color)

        right = ttk.Frame(lower)
        right.pack(side="left", fill="y", padx=(10, 0))
        self.chat = U.ChatPanel(right, on_send=self._send_chat)
        self.chat.pack(fill="both", expand=True)
        self.stats = U.StatBar(right)
        self.stats.pack(fill="x", pady=(8, 0))

        self._sync_controls()

    # ---- controls --------------------------------------------------------
    def _sync_controls(self):
        running = self.server is not None
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.copy_btn.configure(state="normal" if not running else "disabled")

    def _refresh_monitor_list(self):
        self.monitors = self.engine.monitors()
        self.mon_combo.configure(values=[m["name"] for m in self.monitors])

    def _on_monitor_pick(self, _evt=None):
        name = self.mon_var.get()
        for m in self.monitors:
            if m["name"] == name:
                self.engine.configure(monitor_index=m["index"])
                self.log(f"Sharing: {name}", "info")
                break

    def _copy_addr(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.addr_var.get())
        self.log("Address copied to clipboard.", "info")

    # ---- server ----------------------------------------------------------
    def start(self):
        if self.server:
            return
        try:
            port = int(self.port_var.get())
        except ValueError:
            U.show_error(self.root, "Bad port", "Port must be a number.")
            return
        password = self.pw_var.get()
        self.cfg.update({
            "host_port": port, "host_password": password,
            "host_allow_control": self.ctrl_var.get(),
            "host_allow_files": self.files_var.get(),
            "host_allow_clipboard": self.clip_var.get(),
        })
        U.save_settings(self.cfg)
        self.engine.configure(quality=self.cfg["quality"], fps=self.cfg["fps"],
                              scale_pct=self.cfg["scale"])
        for m in self.monitors:
            if m["name"] == self.mon_var.get():
                self.engine.configure(monitor_index=m["index"])
                break

        try:
            self.server = HostServer(
                ("0.0.0.0", port), self.engine, password,
                allow_control=self.ctrl_var.get(),
                allow_files=self.files_var.get(),
                allow_clipboard=self.clip_var.get(),
                on_event=self.enqueue_event,
                host_ui=self,
            )
        except OSError as exc:
            self.server = None
            U.show_error(self.root, "Cannot listen",
                         f"Port {port} is not available:\n{exc}")
            return

        self.server_thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.5},
            daemon=True)
        self.server_thread.start()
        self.stats_started = time.time()
        self.badge.configure(text="● sharing", fg=GREEN)
        self.addr_var.set(f"{U.local_ip()}:{port}")
        self.log(f"Sharing on {U.local_ip()}:{port}"
                 + ("" if not password else " (password required)"), "ok")
        self.log("Waiting for a client to connect...", "info")
        self._sync_controls()

    def stop(self):
        if not self.server:
            return
        session = self.server.session
        if session and session.is_alive():
            try:
                session.send_json(P.BYE, {})
            except P.ConnectionLost:
                pass
            session.stop()
        self.server.shutdown()
        self.server.server_close()
        self.server = None
        self.badge.configure(text="● offline", fg=RED)
        self.chat.set_connected(False)
        self.log("Sharing stopped.", "warn")
        self._sync_controls()

    # ---- events (cross-thread) -------------------------------------------
    def enqueue_event(self, kind: str, data: dict):
        self.event_q.put((kind, data or {}))

    def _poll_events(self):
        self._drain_chat_q()
        try:
            while True:
                kind, data = self.event_q.get_nowait()
                self._handle_event(kind, data)
        except queue.Empty:
            pass
        self.root.after(60, self._poll_events)

    def _handle_event(self, kind, data):
        if kind == "connected":
            addr = data.get("addr", ("?", 0))[0]
            self.log(f"Client connected: {addr}", "ok")
            self.chat.set_connected(True)
            self.stats_clients = 1
        elif kind == "disconnected":
            addr = data.get("addr", ("?", 0))[0]
            self.log(f"Client disconnected: {addr}", "warn")
            self.chat.set_connected(False)
            self.stats_clients = 0
        elif kind == "chat":
            # queued by the session thread via enqueue_chat(); applied on the
            # UI thread by _drain_chat_q (see _poll_events)
            pass
        elif kind == "file_offer":
            self.log(f"Receiving file: {data.get('name')} "
                     f"({U.format_bytes(data.get('size', 0))})", "info")
        elif kind == "file_done":
            self.log(f"Saved: {data.get('path')}", "ok")
        elif kind == "file_progress":
            self._last_file = data

    def enqueue_chat(self, text: str):
        # Called from the session's drain thread. NEVER touch Tk here — even
        # root.after() registers a new Tcl command, which raises
        # "main thread is not in main loop" off the UI thread. Queue it and
        # let _poll_events (running on the UI thread) apply it.
        self.chat_q.put(text)

    def _drain_chat_q(self):
        try:
            while True:
                text = self.chat_q.get_nowait()
                self.chat.add(text, "remote")
        except queue.Empty:
            pass

    def _send_chat(self, text: str):
        if self.server and self.server.session and self.server.session.is_alive():
            try:
                self.server.session.say(text)
            except P.ConnectionLost as exc:
                self.chat.add(f"send failed: {exc}", "err")

    # ---- logging / stats --------------------------------------------------
    def log(self, text: str, tag: str = "info"):
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{stamp}  ", "info")
        self.log_text.insert("end", f"{text}\n", tag)
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _tick_stats(self):
        if self.server and self.server.session and self.server.session.is_alive():
            s = self.server.session
            up = time.time() - self.stats_started
            fps = s.frames_sent / up if up > 0 else 0
            mbps = s.engine.bytes_sent / up / 125000 if up > 0 else 0
            fileinfo = ""
            if getattr(self, "_last_file", None):
                f = self._last_file
                pct = 100 * f["received"] / f["size"] if f.get("size") else 0
                fileinfo = f"  |  recv {f.get('name','')[:18]} {pct:.0f}%"
            self.stats.set(
                f"{s.frames_sent} frames  |  {fps:4.1f} fps  |  "
                f"{mbps:4.2f} MB/s  |  {U.format_bytes(s.engine.bytes_sent)}{fileinfo}"
            )
        else:
            self.stats.set("idle")
        self.root.after(1000, self._tick_stats)

    # ---- shutdown ----------------------------------------------------------
    def on_close(self):
        self.stop()
        U.save_settings(self.cfg)
        self.root.destroy()


# ---------------------------------------------------------------------- headless
def run_headless(port: int, password: str, allow_control=True, allow_files=False,
                 allow_clipboard=False):
    engine = CaptureEngine()
    cfg = U.load_settings()
    engine.configure(quality=cfg["quality"], fps=cfg["fps"], scale_pct=cfg["scale"])

    def on_event(kind, data):
        print(f"[host] {kind}: {data}", flush=True)

    server = HostServer(("0.0.0.0", port), engine, password,
                        allow_control=allow_control, allow_files=allow_files,
                        allow_clipboard=allow_clipboard, on_event=on_event,
                        host_ui=None)
    print(f"[host] listening on 0.0.0.0:{port}"
          + ("" if not password else " (auth on)"), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("[host] stopping...", flush=True)
    finally:
        server.shutdown()
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="P2P remote desktop — host")
    parser.add_argument("--port", type=int, default=None, help="TCP port")
    parser.add_argument("--password", default=None, help="access password")
    parser.add_argument("--no-gui", action="store_true", help="headless mode")
    parser.add_argument("--allow-files", action="store_true",
                       help="headless: accept incoming file transfers")
    parser.add_argument("--allow-clipboard", action="store_true",
                       help="headless: sync clipboard")
    args = parser.parse_args()

    cfg = U.load_settings()
    W.set_dpi_aware()

    if args.no_gui:
        port = args.port if args.port is not None else cfg["host_port"]
        pw = args.password if args.password is not None else cfg["host_password"]
        logging.basicConfig(level=logging.INFO)
        run_headless(port, pw, allow_files=args.allow_files,
                     allow_clipboard=args.allow_clipboard)
        return

    if args.port is not None:
        cfg["host_port"] = args.port
    if args.password is not None:
        cfg["host_password"] = args.password

    root = tk.Tk()
    HostApp(root, cfg)
    root.mainloop()


if __name__ == "__main__":
    main()

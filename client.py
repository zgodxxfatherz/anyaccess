"""
client.py — the viewer side of the P2P remote desktop.

Run this on the machine you want to control FROM:

    python client.py                     # GUI viewer
    python client.py --host HOST_IP  # custom target

The viewer opens ONE TCP connection to the host and demultiplexes it:

    host -> client : AUTH_RESULT, HELLO, FRAME, CHAT, CLIPBOARD, PONG, BYE
    client -> host : AUTH, MOUSE, KEY, CHAT, QUALITY, MONITOR, FILE_*, PING

The socket runs on a reader thread; JPEG frames are posted to a bounded
queue, so a slow GUI decode never applies backpressure to the wire. The
display canvas is 1:1 with the remote screen and lives inside a scrollable,
fullscreen-capable window; mouse/keyboard events are translated back to
remote pixel coordinates before being sent.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk

import protocol as P
import uikit as U
from uikit import (ACCENT, AMBER, BG, FG, GREEN, MUTED, PANEL, PANEL2, RED)

log = logging.getLogger("client")

CHUNK = 65536
FRAME_QUEUE_MAX = 3          # bounded: drop frames rather than buffer forever


# ------------------------------------------------------------- client core
class DesktopClient:
    """Wire-level client: auth, frame pumping, file transfer, keepalive."""

    def __init__(self, host: str, port: int, password: str, on_event=None):
        self.host = host
        self.port = port
        self.password = password or ""
        self.on_event = on_event or (lambda kind, data=None: None)
        self.sock: socket.socket | None = None
        self.send_lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.frames: "queue.Queue[bytes]" = queue.Queue(maxsize=FRAME_QUEUE_MAX)
        self.screen_w = 0
        self.screen_h = 0
        self.monitors = []
        self.connected = False
        self.rtt_ms = None
        self.bytes_in = 0
        self.frames_in = 0
        # outbound file state
        self._send_file_id = 0
        self._pending_send: dict | None = None
        self._send_lock_file = threading.Lock()

    # ---- connection -------------------------------------------------------
    def connect(self, timeout: float = 8.0) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        P.tune_socket(sock)
        self.sock = sock
        try:
            if self.password:
                self.send_json(P.AUTH, {"password": self.password})
            else:
                self.send_json(P.AUTH, {"password": ""})
            mtype, payload = P.recv_msg(sock)
            if mtype != P.AUTH_RESULT:
                raise P.ProtocolError("no AUTH_RESULT from host")
            result = json.loads(payload.decode("utf-8"))
            if not result.get("ok"):
                raise PermissionError(result.get("why") or "access denied")
            mtype, payload = P.recv_msg(sock)
            if mtype == P.HELLO:
                hello = json.loads(payload.decode("utf-8"))
                self.screen_w = hello.get("screen_w", 0)
                self.screen_h = hello.get("screen_h", 0)
                self.monitors = hello.get("monitors", [])
                self.on_event("hello", hello)
            mtype, payload = P.recv_msg(sock)
            if mtype == P.CTRL_STATE:
                self.on_event("ctrl_state", json.loads(payload.decode("utf-8")))
        except Exception:
            self._hard_close()
            raise
        self.connected = True
        self.on_event("connected", {})

        threading.Thread(target=self._reader, name="client-reader",
                         daemon=True).start()
        threading.Thread(target=self._keepalive, name="client-keepalive",
                         daemon=True).start()

    def _hard_close(self) -> None:
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        self.sock = None

    def close(self) -> None:
        self.stop_flag.set()
        if self.sock:
            try:
                with self.send_lock:
                    P.send_msg(self.sock, P.BYE)
            except (P.ConnectionLost, OSError):
                pass
            self._hard_close()
        self.connected = False

    # ---- sending ----------------------------------------------------------
    def send(self, mtype: int, payload=b"") -> None:
        if not self.sock or self.stop_flag.is_set():
            return
        with self.send_lock:
            P.send_msg(self.sock, mtype, payload)

    def send_json(self, mtype: int, obj) -> None:
        self.send(mtype, json.dumps(obj).encode("utf-8"))

    def send_chat(self, text: str) -> None:
        self.send_json(P.CHAT, {"text": text})

    def send_quality(self, quality=None, fps=None, scale=None) -> None:
        msg = {}
        if quality is not None:
            msg["quality"] = quality
        if fps is not None:
            msg["fps"] = fps
        if scale is not None:
            msg["scale"] = scale
        if msg:
            self.send_json(P.QUALITY, msg)

    def send_clipboard(self, text: str) -> None:
        self.send_json(P.CLIPBOARD, {"text": text})

    def select_monitor(self, index: int) -> None:
        self.send_json(P.MONITOR, {"index": index})

    # ---- input forwarding --------------------------------------------------
    def mouse_move(self, x: int, y: int) -> None:
        self.send_json(P.MOUSE, {"type": "move", "x": x, "y": y})

    def mouse_down(self, x: int, y: int, button="left") -> None:
        self.send_json(P.MOUSE, {"type": "down", "x": x, "y": y,
                                 "button": button})

    def mouse_up(self, x: int, y: int, button="left") -> None:
        self.send_json(P.MOUSE, {"type": "up", "x": x, "y": y,
                                 "button": button})

    def wheel(self, delta: int) -> None:
        self.send_json(P.MOUSE, {"type": "wheel", "delta": delta})

    def key(self, vk: int, uni: str = "", up: bool = False) -> None:
        self.send_json(P.KEY, {"vk": vk, "uni": uni,
                               "type": "up" if up else "down"})

    # ---- background threads -------------------------------------------------
    def _reader(self) -> None:
        while not self.stop_flag.is_set():
            try:
                mtype, payload = P.recv_msg(self.sock)
            except (P.ConnectionLost, P.ProtocolError, OSError) as exc:
                log.info("reader stopped: %s", exc)
                break
            if mtype is None:
                break
            try:
                self._dispatch(mtype, payload)
            except Exception as exc:  # noqa: BLE001
                log.warning("dispatch %s failed: %s", mtype, exc)
        self.connected = False
        self.on_event("disconnected", {})

    def _dispatch(self, mtype: int, payload: bytes) -> None:
        if mtype == P.FRAME:
            self.bytes_in += len(payload)
            self.frames_in += 1
            if self.frames.full():
                try:
                    self.frames.get_nowait()   # drop oldest to stay live
                except queue.Empty:
                    pass
            self.frames.put(payload)
        elif mtype == P.CHAT:
            self.on_event("chat", json.loads(payload).get("text", ""))
        elif mtype == P.CLIPBOARD:
            self.on_event("clipboard", json.loads(payload).get("text", ""))
        elif mtype == P.FILE_OFFER:
            self.on_event("file_offer", json.loads(payload))
        elif mtype == P.FILE_ACCEPT:
            self._on_file_accept(json.loads(payload))
        elif mtype == P.FILE_DATA:
            self._on_file_data(payload)
        elif mtype == P.FILE_DONE:
            self.on_event("file_done", json.loads(payload))
        elif mtype == P.PONG:
            try:
                sent = float(payload.decode("ascii"))
                self.rtt_ms = (time.time() - sent) * 1000
            except ValueError:
                pass

    def _keepalive(self) -> None:
        while not self.stop_flag.wait(2.0):
            try:
                self.send(P.PING, f"{time.time()}".encode("ascii"))
            except P.ConnectionLost:
                break

    # ---- file send -----------------------------------------------------------
    def _on_file_accept(self, info: dict) -> None:
        pending = self._pending_send
        if not pending or info.get("id") != pending["id"]:
            return
        if not info.get("ok"):
            self.on_event("error", {"text": f"Host declined: {info.get('why','')}"})
            self._pending_send = None
            self._send_lock_file.release()
            return
        # send from a dedicated thread so frames/chat keep flowing
        threading.Thread(target=self._send_file_worker,
                         args=(pending,), daemon=True).start()

    def _send_file_worker(self, pending: dict) -> None:
        # NOTE: FILE_DATA carries no file id — the host pairs chunks with the
        # offer that is still pending. Only one send may be in flight at a
        # time (enforced by _send_lock_file in offer_file).
        sent = 0
        try:
            with open(pending["path"], "rb") as fh:
                while not self.stop_flag.is_set():
                    chunk = fh.read(CHUNK)
                    if not chunk:
                        break
                    self.send(P.FILE_DATA, chunk)
                    sent += len(chunk)
                    pending["sent"] = sent
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
            self._send_lock_file.release()

    def offer_file(self, path: str) -> None:
        if not self.connected:
            return
        if not os.path.isfile(path):
            self.on_event("error", {"text": f"File not found: {path}"})
            return
        if self._pending_send is not None:
            self.on_event("error",
                          {"text": "Another file transfer is already running"})
            return
        self._send_file_id += 1
        fid = f"s{self._send_file_id}"
        self._pending_send = {
            "id": fid, "path": path,
            "name": os.path.basename(path),
            "size": os.path.getsize(path),
            "sent": 0,
        }
        self.send_json(P.FILE_OFFER, {
            "id": fid, "name": self._pending_send["name"],
            "size": self._pending_send["size"],
        })
        # hold the transfer lock until FILE_DONE (or a decline) so only one
        # file streams at a time; FILE_DATA carries no id, so interleaving
        # two sends would corrupt the stream
        self._send_lock_file.acquire()

    # ---- file receive --------------------------------------------------------
    def _on_file_data(self, payload: bytes) -> None:
        pass    # host is the only receiver in v1; client sends files only


# -------------------------------------------------------------------- GUI
class ViewerApp:
    def __init__(self, root: tk.Tk, cfg: dict):
        self.root = root
        self.cfg = cfg
        self.client: DesktopClient | None = None
        self.photo = None                 # keep a reference (Tk GC trap)
        self.current_frame: Image.Image | None = None
        self.fit_to_window = True
        self.fullscreen = False
        self.mouse_pressed = False
        self.dragging = False
        self.last_move_sent = 0.0
        self._fullscreen_geo = ""
        self._last_mouse_pos = (0, 0)
        self.remote_w = 0
        self.closed = False        # set by on_close(); guards pending events
        self.remote_h = 0

        U.setup_theme(root)
        U.center(root, 1000, 640)
        root.title("P2P Remote Desktop — Viewer")
        root.minsize(640, 420)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_ui()
        self._bind_shortcuts()
        self._pump_frames()
        self._tick_stats()

    # ---- ui ---------------------------------------------------------------
    def _build_ui(self):
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        # connection bar
        bar = ttk.Frame(main, style="Card.TFrame", padding=(10, 8))
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Host", style="Card.Muted.TLabel").pack(side="left")
        self.host_var = tk.StringVar(value=self.cfg["client_host"])
        ttk.Entry(bar, textvariable=self.host_var, width=22).pack(
            side="left", padx=(6, 12))
        ttk.Label(bar, text="Port", style="Card.Muted.TLabel").pack(side="left")
        self.port_var = tk.StringVar(value=str(self.cfg["client_port"]))
        ttk.Entry(bar, textvariable=self.port_var, width=7).pack(
            side="left", padx=(6, 12))
        ttk.Label(bar, text="Password", style="Card.Muted.TLabel").pack(side="left")
        self.pw_var = tk.StringVar(value=self.cfg["client_password"])
        ttk.Entry(bar, textvariable=self.pw_var, width=14, show="•").pack(
            side="left", padx=(6, 12))
        self.connect_btn = ttk.Button(bar, text="Connect", style="Accent.TButton",
                                      command=self.toggle_connect)
        self.connect_btn.pack(side="left")
        self.badge = tk.Label(bar, text="● disconnected", bg=BG, fg=RED,
                              font=("Segoe UI", 10))
        self.badge.pack(side="right")

        # body: screen area + sidebar
        body = ttk.Frame(main)
        body.pack(fill="both", expand=True)

        self.screen_host = ttk.Frame(body, style="Card.TFrame", padding=6)
        self.screen_host.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(self.screen_host, bg="#0b0d10", bd=0,
                                highlightthickness=0, cursor="arrow")
        self.hbar = ttk.Scrollbar(self.screen_host, orient="horizontal",
                                  command=self.canvas.xview)
        self.vbar = ttk.Scrollbar(self.screen_host, orient="vertical",
                                  command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=self.hbar.set,
                              yscrollcommand=self.vbar.set)
        self.vbar.pack(side="right", fill="y")
        self.hbar.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.placeholder = ttk.Label(
            self.canvas, text="Not connected\n\nEnter a host and press Connect",
            style="Card.TLabel", justify="center")
        self.canvas.create_window(20, 20, window=self.placeholder, anchor="nw")

        side = ttk.Frame(body)
        side.pack(side="left", fill="y", padx=(8, 0))

        self.chat = U.ChatPanel(side, on_send=self._send_chat)
        self.chat.pack(fill="x", pady=(0, 8))

        tools = ttk.Frame(side, style="Card.TFrame", padding=10)
        tools.pack(fill="x", pady=(0, 8))
        ttk.Label(tools, text="Stream", style="Card.TLabel").pack(anchor="w")
        self.q_slider = U.LabeledSlider(tools, "Quality", 10, 95,
                                        self.cfg["quality"],
                                        command=self._on_quality)
        self.q_slider.pack(fill="x", pady=2)
        self.f_slider = U.LabeledSlider(tools, "FPS", 1, 60,
                                        self.cfg["fps"],
                                        command=self._on_fps)
        self.f_slider.pack(fill="x", pady=2)
        self.s_slider = U.LabeledSlider(tools, "Scale", 25, 100,
                                        self.cfg["scale"],
                                        command=self._on_scale)
        self.s_slider.pack(fill="x", pady=2)

        mon_row = ttk.Frame(tools, style="Card.TFrame")
        mon_row.pack(fill="x", pady=(8, 0))
        ttk.Label(mon_row, text="Monitor", style="Card.Muted.TLabel").pack(
            side="left", padx=(0, 6))
        self.mon_var = tk.StringVar()
        self.mon_combo = ttk.Combobox(mon_row, textvariable=self.mon_var,
                                      state="readonly", width=16)
        self.mon_combo.pack(side="left")
        self.mon_combo.bind("<<ComboboxSelected>>", self._on_monitor_pick)

        view_row = ttk.Frame(tools, style="Card.TFrame")
        view_row.pack(fill="x", pady=(8, 0))
        self.fit_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(view_row, text="Fit to window", variable=self.fit_var,
                        command=self._on_fit_toggle).pack(side="left")
        self.fs_btn = ttk.Button(view_row, text="Fullscreen",
                                 command=self.toggle_fullscreen)
        self.fs_btn.pack(side="right")

        file_row = ttk.Frame(tools, style="Card.TFrame")
        file_row.pack(fill="x", pady=(8, 0))
        self.send_file_btn = ttk.Button(file_row, text="Send file to remote…",
                                        command=self._pick_and_send,
                                        state="disabled")
        self.send_file_btn.pack(side="left")
        self.file_status = ttk.Label(file_row, text="", style="Card.Muted.TLabel")
        self.file_status.pack(side="left", padx=(8, 0))

        self.stats = U.StatBar(side)
        self.stats.pack(fill="x")

        # canvas events
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<ButtonPress-1>", self._on_button_press)
        self.canvas.bind("<ButtonRelease-1>", self._on_button_release)
        self.canvas.bind("<ButtonPress-3>", self._on_button_press_r)
        self.canvas.bind("<ButtonRelease-3>", self._on_button_release_r)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-2>", lambda e: "break")
        self.canvas.bind("<Enter>", lambda e: self.canvas.focus_set())

    # ---- shortcuts ----------------------------------------------------------
    def _bind_shortcuts(self):
        self.root.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.root.bind("<Escape>", self._on_escape)
        self.root.bind("<F5>", lambda e: self._reconnect())
        self.root.bind("<Control-l>", lambda e: self.toggle_connect())

    def _on_escape(self, _evt=None):
        if self.fullscreen:
            self.toggle_fullscreen()

    # ---- connect ------------------------------------------------------------
    def toggle_connect(self):
        if self.client and self.client.connected:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        host = self.host_var.get().strip()
        try:
            port = int(self.port_var.get())
        except ValueError:
            U.show_error(self.root, "Bad port", "Port must be a number.")
            return
        if not host:
            U.show_error(self.root, "Missing host", "Enter the remote address.")
            return

        self.cfg.update({
            "client_host": host, "client_port": port,
            "client_password": self.pw_var.get(),
            "quality": self.q_slider.get(), "fps": self.f_slider.get(),
            "scale": self.s_slider.get(),
        })
        U.save_settings(self.cfg)

        self.set_busy(True)
        self.connect_btn.configure(text="Connecting…", state="disabled")
        threading.Thread(target=self._connect_worker,
                         args=(host, port), daemon=True).start()

    def _connect_worker(self, host: str, port: int):
        client = DesktopClient(host, port, self.pw_var.get(), on_event=self._on_event)
        try:
            client.connect(timeout=8.0)
        except Exception as exc:  # noqa: BLE001 - surface any failure to user
            self.root.after(0, self._connect_failed, str(exc))
            return
        self.client = client
        self.root.after(0, self._connect_ok)

    def _connect_ok(self):
        self.badge.configure(text="● connected", fg=GREEN)
        self.connect_btn.configure(text="Disconnect", state="normal")
        self.send_file_btn.configure(state="normal")
        self.chat.set_connected(True)
        if self.client and self.client.monitors:
            self.mon_combo.configure(
                values=[m.get("name", "?") for m in self.client.monitors])
            self.mon_combo.current(0)
        self._apply_quality(initial=True)

    def _connect_failed(self, message: str):
        self.badge.configure(text="● disconnected", fg=RED)
        self.connect_btn.configure(text="Connect", state="normal")
        self.set_busy(False)
        U.show_error(self.root, "Connection failed", message)

    def disconnect(self):
        if self.client:
            self.client.close()
        self.client = None
        self.badge.configure(text="● disconnected", fg=RED)
        self.connect_btn.configure(text="Connect", state="normal")
        self.send_file_btn.configure(state="disabled")
        self.chat.set_connected(False)
        self._clear_screen()

    def _reconnect(self):
        if self.client and self.client.connected:
            self.disconnect()
            self.root.after(200, self.connect)

    def _on_event(self, kind: str, data):
        """Events from the client's background threads -> hop to the UI."""
        if kind in ("chat", "error", "clipboard", "file_offer", "send_progress",
                    "file_done"):
            self.root.after(0, self._handle_ui_event, kind, data)
        elif kind == "hello":
            self.root.after(0, self._handle_hello, data)
        elif kind == "disconnected":
            self.root.after(0, self._handle_disconnect)

    # ---- ui events ---------------------------------------------------------
    def _handle_hello(self, hello: dict):
        self.remote_w = hello.get("screen_w", 0)
        self.remote_h = hello.get("screen_h", 0)
        if self.client and self.client.monitors:
            self.mon_combo.configure(
                values=[m.get("name", "?") for m in self.client.monitors])
            self.mon_combo.current(0)

    def _handle_disconnect(self):
        self.badge.configure(text="● disconnected", fg=RED)
        self.connect_btn.configure(text="Connect", state="normal")
        self.send_file_btn.configure(state="disabled")
        self.chat.set_connected(False)
        self._clear_screen()

    def _handle_ui_event(self, kind, data):
        if kind == "chat":
            self.chat.add(data, "remote")
        elif kind == "error":
            self.chat.add(data.get("text", ""), "err")
            self.file_status.configure(text="", foreground=MUTED)
        elif kind == "clipboard":
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(data)
            except tk.TclError:
                pass
        elif kind == "send_progress":
            d = data
            pct = 100 * d["sent"] / d["size"] if d.get("size") else 0
            self.file_status.configure(
                text=f"{d['name'][:20]}… {pct:.0f}% "
                     f"({U.format_bytes(d['sent'])}/{U.format_bytes(d['size'])})")
        elif kind == "file_done":
            self.file_status.configure(text="sent ✓", foreground=GREEN)

    def _send_chat(self, text: str):
        if self.client and self.client.connected:
            self.client.send_chat(text)

    def set_busy(self, busy: bool):
        self.root.configure(cursor="watch" if busy else "")

    # ---- stream controls ----------------------------------------------------
    def _apply_quality(self, initial=False):
        if not self.client or not self.client.connected:
            return
        if not initial:
            self.cfg["quality"] = self.q_slider.get()
            self.cfg["fps"] = self.f_slider.get()
            self.cfg["scale"] = self.s_slider.get()
            U.save_settings(self.cfg)
        self.client.send_quality(quality=self.q_slider.get(),
                                 fps=self.f_slider.get(),
                                 scale=self.s_slider.get())

    def _on_quality(self, _v=None):
        if self.client and self.client.connected:
            self.client.send_quality(quality=self.q_slider.get())
            self.cfg["quality"] = self.q_slider.get()
            U.save_settings(self.cfg)

    def _on_fps(self, _v=None):
        if self.client and self.client.connected:
            self.client.send_quality(fps=self.f_slider.get())
            self.cfg["fps"] = self.f_slider.get()
            U.save_settings(self.cfg)

    def _on_scale(self, _v=None):
        if self.client and self.client.connected:
            self.client.send_quality(scale=self.s_slider.get())
            self.cfg["scale"] = self.s_slider.get()
            U.save_settings(self.cfg)

    def _on_monitor_pick(self, _evt=None):
        if not self.client or not self.client.connected:
            return
        name = self.mon_var.get()
        for m in self.client.monitors:
            if m.get("name") == name:
                self.client.select_monitor(m["index"])
                break

    def _on_fit_toggle(self):
        self.fit_to_window = self.fit_var.get()
        self._render_current()

    # ---- display --------------------------------------------------------------
    def _clear_screen(self):
        self.canvas.delete("all")
        self.canvas.configure(scrollregion=(0, 0, 100, 100))
        self.current_frame = None
        self.photo = None
        self.canvas.create_window(20, 20, window=self.placeholder, anchor="nw")

    def _pump_frames(self):
        """Drain the frame queue on the UI thread; render at most ~30 fps."""
        if self.client and self.client.connected:
            try:
                while True:
                    jpeg = self.client.frames.get_nowait()
                    self._render_frame(jpeg)
            except queue.Empty:
                pass
        self.root.after(16, self._pump_frames)

    def _render_frame(self, jpeg: bytes):
        try:
            img = Image.open(io_bytes(jpeg))
            img.load()
        except Exception:  # noqa: BLE001 - corrupt/partial frames are skipped
            return
        self.current_frame = img
        self._render_current()

    def _render_current(self):
        if self.current_frame is None:
            return
        img = self.current_frame
        self.remote_w, self.remote_h = img.size
        vw = max(self.canvas.winfo_width() - 4, 50)
        vh = max(self.canvas.winfo_height() - 4, 50)

        if self.fit_to_window:
            factor = min(vw / img.width, vh / img.height)
            if factor < 1.0:
                size = (max(1, int(img.width * factor)),
                        max(1, int(img.height * factor)))
                img = img.resize(size, Image.BILINEAR)
            self.canvas.configure(scrollregion=(0, 0, img.width, img.height))
        else:
            self.canvas.configure(scrollregion=(0, 0, img.width, img.height))

        self.photo = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

    # ---- input ---------------------------------------------------------------
    def _canvas_to_remote(self, x_root: int, y_root: int):
        """Translate canvas position -> remote desktop pixel coords."""
        if self.current_frame is None:
            return None
        cx = self.canvas.canvasx(x_root)
        cy = self.canvas.canvasy(y_root)
        img_w, img_h = self.photo.width(), self.photo.height()
        if img_w == 0 or img_h == 0:
            return None
        rx = int(cx * self.remote_w / img_w)
        ry = int(cy * self.remote_h / img_h)
        return max(0, min(self.remote_w - 1, rx)), max(0, min(self.remote_h - 1, ry))

    def _on_motion(self, evt):
        if not self.client or not self.client.connected:
            return
        now = time.monotonic()
        if now - self.last_move_sent < 0.016:   # cap at ~60 Hz
            return
        self.last_move_sent = now
        pos = self._canvas_to_remote(evt.x, evt.y)
        if pos:
            self.client.mouse_move(*pos)

    def _on_button_press(self, evt):
        if not self.client or not self.client.connected:
            return
        pos = self._canvas_to_remote(evt.x, evt.y)
        if pos:
            self.client.mouse_down(*pos, button="left")

    def _on_button_press_r(self, evt):
        if not self.client or not self.client.connected:
            return
        pos = self._canvas_to_remote(evt.x, evt.y)
        if pos:
            self.client.mouse_down(*pos, button="right")

    def _on_button_release(self, evt):
        if not self.client or not self.client.connected:
            return
        pos = self._canvas_to_remote(evt.x, evt.y)
        if pos:
            self.client.mouse_up(*pos, button="left")

    def _on_button_release_r(self, evt):
        if not self.client or not self.client.connected:
            return
        pos = self._canvas_to_remote(evt.x, evt.y)
        if pos:
            self.client.mouse_up(*pos, button="right")

    def _on_wheel(self, evt):
        if not self.client or not self.client.connected:
            return
        delta = 120 if evt.delta > 0 else -120
        self.client.wheel(delta)

    def _on_canvas_key(self, evt):
        if not self.client or not self.client.connected:
            return
        uni = evt.char if evt.char and ord(evt.char[0]) >= 32 else ""
        vk = VK_MAP.get(evt.keysym.lower(), 0)
        if uni:
            self.client.key(0, uni=uni)
        elif vk:
            self.client.key(vk)

    # ---- fullscreen ----------------------------------------------------------
    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        self.root.attributes("-fullscreen", self.fullscreen)
        if self.fullscreen:
            self._fullscreen_geo = self.root.winfo_geometry()
            self.root.geometry(f"{self.root.winfo_screenwidth()}x"
                               f"{self.root.winfo_screenheight()}+0+0")
            self.root.configure(cursor="none")
        else:
            self.root.geometry(self._fullscreen_geo or "1000x640")
            self.root.configure(cursor="")
        self.root.after(50, self._render_current)

    # ---- file transfer --------------------------------------------------------
    def _pick_and_send(self):
        path = U.pick_open_path(self.root)
        if path and self.client:
            self.file_status.configure(text="sending…", foreground=MUTED)
            self.client.offer_file(path)

    # ---- stats -----------------------------------------------------------------
    def _tick_stats(self):
        if self.client and self.client.connected:
            c = self.client
            rtt = f"{c.rtt_ms:5.0f} ms" if c.rtt_ms is not None else "   — ms"
            self.stats.set(
                f"{c.frames_in} frames  |  "
                f"{U.format_bytes(c.bytes_in)}  |  rtt {rtt}"
            )
        else:
            self.stats.set("idle")
        self.root.after(1000, self._tick_stats)

    # ---- shutdown -------------------------------------------------------------
    def on_close(self):
        self.closed = True        # stop pending UI handlers touching widgets
        if self.client:
            self.client.close()
        U.save_settings(self.cfg)
        self.root.destroy()


# ---------------------------------------------------------------- helpers
def io_bytes(data: bytes):
    import io
    return io.BytesIO(data)


VK_MAP = {
    "return": 0x0D, "tab": 0x09, "backspace": 0x08, "escape": 0x1B,
    "delete": 0x2E, "space": 0x20, "home": 0x24, "end": 0x23,
    "prior": 0x21, "next": 0x22, "left": 0x25, "up": 0x26,
    "right": 0x27, "down": 0x28, "insert": 0x2D,
    "shift_l": 0xA0, "shift_r": 0xA1, "control_l": 0xA2,
    "control_r": 0xA3, "alt_l": 0xA4, "alt_r": 0xA5,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B,
}


def main():
    parser = argparse.ArgumentParser(description="P2P remote desktop — viewer")
    parser.add_argument("--host", default=None, help="remote host/IP")
    parser.add_argument("--port", type=int, default=None, help="remote port")
    args = parser.parse_args()

    cfg = U.load_settings()
    if args.host:
        cfg["client_host"] = args.host
    if args.port:
        cfg["client_port"] = args.port

    root = tk.Tk()
    app = ViewerApp(root, cfg)
    # keyboard forwarding: canvas grabs focus on click, root catches the rest
    root.bind("<KeyPress>", app._on_canvas_key)
    root.mainloop()


if __name__ == "__main__":
    main()

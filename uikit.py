"""
uikit.py — shared Tkinter widgets, dark theme, settings persistence and
small helpers used by both host.py and client.py.
"""
from __future__ import annotations

import json
import os
import socket
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

SETTINGS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "p2p_settings.json"
)

DEFAULTS = {
    # host side
    "host_port": 47800,
    "host_password": "",
    "host_allow_control": True,
    "host_allow_files": True,
    "host_allow_clipboard": True,
    # shared stream settings
    "quality": 70,          # JPEG quality 10..95
    "fps": 30,              # target frames per second
    "scale": 100,           # downscale percent (100 = 1:1 pixels)
    "auto_quality": True,   # lower quality when the link is slow
    # client side
    "client_host": "127.0.0.1",
    "client_port": 47800,
    "client_password": "",
    "downloads_dir": "",
}

# ------------------------------------------------------------------- palette
BG = "#16181d"
PANEL = "#1f232b"
PANEL2 = "#262b34"
BORDER = "#333a45"
FG = "#e6e8eb"
MUTED = "#98a0ac"
ACCENT = "#3b82f6"
ACCENT_DK = "#2563eb"
GREEN = "#22c55e"
RED = "#ef4444"
AMBER = "#f59e0b"


def setup_theme(root: tk.Tk) -> ttk.Style:
    """Apply a compact dark theme to every ttk widget in the app."""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    base = {
        "TFrame": {"background": BG},
        "Card.TFrame": {"background": PANEL},
        "TLabel": {"background": BG, "foreground": FG},
        "Card.TLabel": {"background": PANEL, "foreground": FG},
        "Muted.TLabel": {"background": BG, "foreground": MUTED},
        "Card.Muted.TLabel": {"background": PANEL, "foreground": MUTED},
        "TButton": {
            "background": PANEL2, "foreground": FG, "borderwidth": 1,
            "focusthickness": 0, "padding": 6,
        },
        "TButton:hover": {"background": "#2f3542"},
        "Accent.TButton": {
            "background": ACCENT, "foreground": "#ffffff", "borderwidth": 0,
            "focusthickness": 0, "padding": 8,
        },
        "Accent.TButton:hover": {"background": ACCENT_DK},
        "Danger.TButton": {
            "background": "#3a2326", "foreground": "#f2b8b5", "borderwidth": 1,
            "focusthickness": 0, "padding": 8,
        },
        "TEntry": {
            "fieldbackground": PANEL2, "foreground": FG, "insertcolor": FG,
            "borderwidth": 1, "padding": 5,
        },
        "TCheckbutton": {
            "background": PANEL, "foreground": FG, "focusthickness": 0,
        },
        "TCombobox": {
            "fieldbackground": PANEL2, "background": PANEL2, "foreground": FG,
            "arrowcolor": FG, "borderwidth": 1, "padding": 4,
        },
        "TScale": {"background": PANEL, "troughcolor": PANEL2},
        "TProgressbar": {"background": ACCENT, "troughcolor": PANEL2},
        "Vertical.TScrollbar": {
            "background": PANEL2, "troughcolor": BG, "borderwidth": 0,
            "arrowcolor": MUTED,
        },
        "Horizontal.TScrollbar": {
            "background": PANEL2, "troughcolor": BG, "borderwidth": 0,
            "arrowcolor": MUTED,
        },
        "TNotebook": {"background": BG, "borderwidth": 0},
        "TNotebook.Tab": {
            "background": PANEL, "foreground": MUTED, "padding": [14, 6],
        },
    }
    for name, opts in base.items():
        style.configure(name, **opts)
    style.map("TCombobox", fieldbackground=[("readonly", PANEL2)])
    style.map("TCheckbutton", background=[("active", PANEL)])
    style.map("TNotebook.Tab", background=[("selected", PANEL2)],
              foreground=[("selected", FG)])
    root.configure(bg=BG)
    return style


# ------------------------------------------------------------------ settings
def load_settings() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            cfg.update({k: v for k, v in stored.items() if k in cfg})
    except (OSError, ValueError):
        pass
    return cfg


def save_settings(cfg: dict) -> None:
    clean = {k: v for k, v in cfg.items() if k in DEFAULTS}
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, indent=2)
    except OSError:
        pass


# ------------------------------------------------------------------ helpers
def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def format_speed(bps: float) -> str:
    return f"{format_bytes(bps)}/s"


def local_ip() -> str:
    """Best-effort LAN IP (no traffic is actually sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        ip = sock.getsockname()[0]
    except OSError:
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            ip = "127.0.0.1"
    finally:
        sock.close()
    return ip


def pick_save_path(parent, filename: str, initial_dir: str = "") -> str:
    os.makedirs(initial_dir, exist_ok=True) if initial_dir else None
    path = filedialog.asksaveasfilename(
        parent=parent,
        title="Save received file",
        initialfile=filename,
        initialdir=initial_dir or os.path.expanduser("~"),
        defaultextension="",
    )
    return path or ""


def pick_open_path(parent) -> str:
    return filedialog.askopenfilename(parent=parent, title="Choose a file to send") or ""


def show_error(parent, title: str, message: str) -> None:
    messagebox.showerror(title, message, parent=parent)


def center(win: tk.Tk, width: int, height: int) -> None:
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = max(0, (sw - width) // 2)
    y = max(0, (sh - height) // 3)
    win.geometry(f"{width}x{height}+{x}+{y}")


# ------------------------------------------------------------------ widgets
class ChatPanel(ttk.Frame):
    """Scrollable transcript + composer. Caller wires on_send to the wire."""

    def __init__(self, parent, on_send=None, title="Chat", height=170):
        super().__init__(parent, style="Card.TFrame", padding=8)
        self.on_send = on_send
        self._enabled = False

        head = ttk.Frame(self, style="Card.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text=title, style="Card.TLabel").pack(side="left")
        self.status = ttk.Label(head, text="not connected", style="Card.Muted.TLabel")
        self.status.pack(side="right")

        body = ttk.Frame(self, style="Card.TFrame")
        body.pack(fill="both", expand=True, pady=(6, 4))

        self.text = tk.Text(
            body, height=6, bg=PANEL2, fg=FG, insertbackground=FG,
            relief="flat", wrap="word", state="disabled",
            font=("Segoe UI", 9), padx=8, pady=6, spacing1=2, spacing3=2,
        )
        scroll = ttk.Scrollbar(body, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        for tag, color in (("me", ACCENT), ("remote", GREEN),
                           ("sys", MUTED), ("err", RED)):
            self.text.tag_configure(tag, foreground=color)
        self.text.tag_configure("remote", foreground=GREEN)

        bar = ttk.Frame(self, style="Card.TFrame")
        bar.pack(fill="x")
        self.entry = ttk.Entry(bar, font=("Segoe UI", 10))
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: self._submit())
        self.send_btn = ttk.Button(bar, text="Send", width=8,
                                   command=self._submit, state="disabled")
        self.send_btn.pack(side="left", padx=(6, 0))

    # -- public API -------------------------------------------------------
    def set_connected(self, connected: bool) -> None:
        self._enabled = connected
        self.send_btn.configure(state="normal" if connected else "disabled")
        self.status.configure(
            text="connected" if connected else "not connected",
            foreground=GREEN if connected else MUTED,
        )
        self.add("Chat is ready." if connected else "Chat closed.", "sys")

    def add(self, text: str, who: str = "sys") -> None:
        """who: me | remote | sys | err"""
        label = {"me": "You", "remote": "Remote", "sys": "•", "err": "!"}[who]
        stamp = ""
        self.text.configure(state="normal")
        if stamp:
            self.text.insert("end", f"{stamp} ", "sys")
        self.text.insert("end", f"{label}: " if who in ("me", "remote") else f"{label} ",
                         who)
        self.text.insert("end", f"{text}\n", who)
        self.text.configure(state="disabled")
        self.text.see("end")

    def _submit(self) -> None:
        if not self._enabled:
            return
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self.add(text, "me")
        if self.on_send:
            try:
                self.on_send(text)
            except Exception as exc:  # noqa: BLE001 - surface wire errors in chat
                self.add(f"send failed: {exc}", "err")


class StatBar(ttk.Frame):
    """Small monospace status strip used by both apps."""

    def __init__(self, parent):
        super().__init__(parent, style="Card.TFrame", padding=(10, 5))
        self.var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.var, style="Card.Muted.TLabel",
                  font=("Consolas", 9)).pack(side="left")

    def set(self, text: str) -> None:
        self.var.set(text)


class LabeledSlider(ttk.Frame):
    """Slider + live value readout, e.g. quality/fps/scale."""

    def __init__(self, parent, label, from_, to, value, command=None, fmt=None):
        super().__init__(parent, style="Card.TFrame")
        self.fmt = fmt or (lambda v: f"{int(float(v))}")
        self.command = command
        ttk.Label(self, text=label, style="Card.TLabel", width=9).pack(side="left")
        self.readout = ttk.Label(self, text=self.fmt(value), style="Card.Muted.TLabel",
                                 width=5)
        self.readout.pack(side="right")
        self.scale = ttk.Scale(self, from_=from_, to=to, orient="horizontal",
                               command=self._moved)
        self.scale.set(value)
        self.scale.pack(side="left", fill="x", expand=True, padx=(2, 8))

    def _moved(self, value):
        self.readout.configure(text=self.fmt(value))
        if self.command:
            self.command(float(value))

    def get(self):
        return int(float(self.scale.get()))

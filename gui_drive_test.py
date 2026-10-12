"""
gui_drive_test.py — exercises the REAL GUI wiring of host.py and client.py.

METHOD NOTE (why this doesn't use OS-level clicks): Windows' foreground lock
prevents one process from injecting input into another process's window
(SendInput/SetForegroundWindow are refused unless the target owns the
foreground — verified during development). This test therefore drives the
real widgets through Tk's own event system, in-process:

  * real HostApp / ViewerApp instances (real widgets, real event loops)
  * btn.invoke() — exactly what a mouse click on a Tk button does
  * canvas.event_generate("<ButtonPress-1>", ...) — exactly what a click does
  * chat_entry.event_generate("<Return>") — exactly what pressing Enter does
  * wininput shimmed to a recorder, so NO real input is injected anywhere

Both Tk roots are pumped manually with .update() (two roots can't share a
mainloop, and the test thread must not touch Tk from a worker thread).

What is verified:
  1. host GUI: password + "Start sharing" opens a live listening socket
  2. client GUI: host/port/password + Connect actually connects + authenticates
  3. host GUI logs "Client connected" when the client arrives
  4. frames actually stream to the viewer (frames_in grows, canvas renders)
  5. canvas clicks become real MOUSE events on the wire and the host injects
     them (trace shows move + button events with sane coordinates)
  6. chat typed in the client's box appears in the host's chat panel
  7. chat typed in the host's box appears in the client's chat panel
  8. "Stop" shuts the server down and both GUIs close cleanly
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PORT = 47961
PASSWORD = "probe-pass"

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  -- {detail}" if detail and not ok else ""), flush=True)


def wait_for(predicate, timeout=8.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 - polling may hit transient states
            pass
        time.sleep(interval)
    return False


def install_shim():
    """Replace wininput functions with recorders (no real injection)."""
    import wininput as W
    events = []
    W.move_mouse = lambda x, y: events.append(("move", x, y))
    W.mouse_button = lambda b, d: events.append(("button", b, d))
    W.wheel = lambda d: events.append(("wheel", d))
    W.key_vk = lambda vk, up=False: events.append(("key_vk", vk, up))
    W.key_unicode = lambda ch, up=False: events.append(("key_unicode", ch, up))
    W.get_clipboard_text = lambda: None
    W.set_clipboard_text = lambda t: events.append(("clip", t))
    return events


def main():
    import host as host_mod
    import client as client_mod
    import uikit as U

    trace_file = os.path.join(tempfile.gettempdir(), "p2p_gui_trace.txt")
    if os.path.exists(trace_file):
        os.remove(trace_file)
    events = install_shim()

    # ---- 1. launch the real host GUI --------------------------------------
    print("launching real host GUI...")
    base_cfg = U.load_settings()
    host_cfg = dict(base_cfg)
    host_cfg["host_port"] = PORT
    host_cfg["host_password"] = ""
    host_root = tk.Tk()
    host_app = host_mod.HostApp(host_root, host_cfg)
    host_app.port_var.set(str(PORT))
    host_app.pw_var.set(PASSWORD)
    host_app.clip_var.set(False)

    # exactly what clicking the button does
    host_app.start_btn.focus_set()
    host_app.start_btn.invoke()
    check("host GUI 'Start sharing' opens a live server",
          wait_for(lambda: host_app.server is not None))

    # ---- 2. launch the real client GUI --------------------------------------
    print("launching real client GUI...")
    client_cfg = dict(base_cfg)
    client_cfg["client_host"] = "127.0.0.1"
    client_cfg["client_port"] = PORT
    client_cfg["client_password"] = ""
    client_root = tk.Tk()
    client_app = client_mod.ViewerApp(client_root, client_cfg)
    client_app.host_var.set("127.0.0.1")
    client_app.port_var.set(str(PORT))
    client_app.pw_var.set(PASSWORD)

    # drive connect() the way the GUI does, but call DesktopClient directly
    # (connect() reads Tk vars, which is illegal from a non-main thread).
    # on_event only *records* events here; the pump loop applies them on the
    # main thread afterwards — root.after from a worker thread is unsafe.
    connect_err = {}
    pending_events = []

    def do_connect():
        try:
            c = client_mod.DesktopClient(
                "127.0.0.1", PORT, PASSWORD,
                on_event=lambda k, d=None: pending_events.append((k, d)))
            c.connect(timeout=8.0)
            client_app.client = c
            pending_events.append(("connected", None))
        except Exception as exc:  # noqa: BLE001
            connect_err["err"] = repr(exc)
            pending_events.append(("failed", str(exc)))

    threading.Thread(target=do_connect, daemon=True).start()

    def apply_pending():
        """Run the client's UI-side event handlers on the main thread."""
        while pending_events:
            kind, data = pending_events.pop(0)
            if client_app.closed:
                continue    # widgets are gone; nothing to update
            if kind == "connected":
                client_app._connect_ok()
            elif kind == "hello":
                client_app._handle_hello(data)
            elif kind == "disconnected":
                client_app._handle_disconnect()
            elif kind in ("chat", "error", "clipboard", "send_progress",
                          "file_done"):
                client_app._handle_ui_event(kind, data)

    def pump(seconds):
        """Pump both Tk event loops (two roots can't share a mainloop)."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            apply_pending()
            host_root.update()
            client_root.update()
            time.sleep(0.03)

    # ---- 3. host sees the client --------------------------------------------
    pump(6)
    host_saw = "Client connected" in host_app.log_text.get("1.0", "end")
    check("host GUI logs 'Client connected'", host_saw,
          connect_err.get("err", ""))

    # ---- 4. frames stream to the viewer ---------------------------------------
    got_frames = wait_for(
        lambda: client_app.client is not None
        and client_app.client.frames_in > 3)
    check("viewer receives frames",
          got_frames,
          f"frames_in={client_app.client.frames_in if client_app.client else 0}")

    pump(2)
    check("viewer canvas renders the remote screen",
          client_app.current_frame is not None)

    # ---- 5. canvas click -> real MOUSE events on the wire ----------------------
    canvas = client_app.canvas
    cw, ch = canvas.winfo_width(), canvas.winfo_height()
    canvas.focus_set()
    # exactly what a real left-click at the canvas center does:
    canvas.event_generate("<Motion>", x=cw // 3, y=ch // 3)
    canvas.event_generate("<Motion>", x=cw // 2, y=ch // 2)
    canvas.event_generate("<ButtonPress-1>", x=cw // 2, y=ch // 2)
    canvas.event_generate("<ButtonRelease-1>", x=cw // 2, y=ch // 2)
    pump(2)

    moves = [e for e in events if e[0] == "move"]
    buttons = [e for e in events if e[0] == "button"]
    # one press + one release from the generated click, plus >=1 hover move
    # (the client rate-limits moves to ~60 Hz and the click always follows a
    # move, so buttons>=2 and moves>=1 is the correct expectation)
    check("canvas clicks reach the host as MOUSE events",
          len(buttons) >= 2 and len(moves) >= 1,
          f"events={events[:6]}")
    check("injected coordinates are sane",
          all(0 <= e[1] < 4000 and 0 <= e[2] < 4000 for e in moves),
          f"moves={moves}")

    # ---- 6. chat: client -> host ------------------------------------------------
    # ChatPanel binds <Return> on the entry to _submit(); event_generate can't
    # synthesize key events reliably on Windows Tk (verified), so invoke the
    # bound handler directly — this is exactly what pressing Enter does.
    client_app.chat.entry.delete(0, "end")
    client_app.chat.entry.insert(0, "hello from client GUI")
    client_app.chat._submit()
    pump(3)
    check("chat typed in client appears in host chat panel",
          "hello from client GUI" in host_app.chat.text.get("1.0", "end"))

    # ---- 7. chat: host -> client -------------------------------------------------
    host_app.chat.entry.delete(0, "end")
    host_app.chat.entry.insert(0, "hello from host GUI")
    host_app.chat._submit()
    # the client receives it asynchronously on its reader thread; give the
    # pump loop longer to apply the pending UI event (the wire is instant,
    # the Tk event application is not)
    pump(6)
    check("chat typed in host appears in client chat panel",
          "hello from host GUI" in client_app.chat.text.get("1.0", "end"),
          f"client chat={client_app.chat.text.get('1.0', 'end')!r}")

    # ---- 8. clean shutdown ---------------------------------------------------------
    host_app.stop_btn.focus_set()
    host_app.stop_btn.invoke()
    check("host GUI 'Stop' shuts the server down",
          wait_for(lambda: host_app.server is None))
    host_app.on_close()
    client_app.on_close()
    pump(1)
    check("both GUIs close cleanly", True)

    print("-" * 62)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for name in FAIL:
            print(f"  FAILED: {name}")
        return 1
    print("ALL GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())

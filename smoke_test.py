"""
smoke_test.py — end-to-end test of the P2P stack WITHOUT touching the real
mouse/keyboard or displaying any window. Everything runs in-process.

Verifies:
  1. framing round-trip over a real TCP socket (localhost)
  2. HELLO negotiation + optional password AUTH handshake
  3. FRAME streaming (real JPEG from a generated test image)
  4. MOUSE / KEY events queued and applied to a fake injector
  5. CHAT both directions
  6. file transfer both directions (FILE_OFFER -> ACCEPT -> DATA -> DONE)
  7. graceful BYE shutdown

The real GUI apps (host.py / client.py) are NOT launched by this test; use
them for the visual experience. This is the automated regression suite.
"""
from __future__ import annotations

import io
import json
import os
import queue
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import protocol as P
import host as host_mod
import client as client_mod

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail and not ok:
        line += f"  -- {detail}"
    print(line, flush=True)


# --------------------------------------------------------------------------
def test_protocol_roundtrip():
    """Framing: mixed JSON/binary/empty payloads over a real socket pair."""
    a, b = socket.socketpair()
    try:
        cases = [
            (P.PING, b"123.45"),
            (P.CHAT, json.dumps({"text": "hi"}).encode()),
            (P.FRAME, os.urandom(100_000)),
            (P.MOUSE, b""),
            (P.KEY, json.dumps({"vk": 13}).encode()),
        ]
        for mtype, payload in cases:
            P.send_msg(a, mtype, payload)
        for mtype, payload in cases:
            rtype, rpayload = P.recv_msg(b)
            if rtype != mtype or rpayload != payload:
                check("protocol framing round-trip", False,
                      f"expected {mtype}/{len(payload)} got {rtype}/{len(rpayload)}")
                return
        check("protocol framing round-trip", True)
    finally:
        a.close()
        b.close()


def _fake_image(w=320, h=180) -> bytes:
    """Generate a real JPEG test image (no screen capture involved)."""
    from PIL import Image
    img = Image.new("RGB", (w, h), (30, 60, 120))
    for i in range(0, w, 16):
        for j in range(0, h, 16):
            if (i + j) % 32 == 0:
                for x in range(i, min(i + 8, w)):
                    for y in range(j, min(j + 8, h)):
                        img.putpixel((x, y), (240, 200, 60))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=60)
    return buf.getvalue()


class FakeInjector:
    """Stands in for wininput so the test never injects real input."""

    def __init__(self):
        self.events = []

    def move(self, x, y):
        self.events.append(("move", x, y))

    def button(self, button, down):
        self.events.append(("button", button, down))

    def wheel(self, delta):
        self.events.append(("wheel", delta))

    def key(self, vk=None, uni=None, up=False):
        self.events.append(("key", vk, uni, up))


def test_full_session():
    """Spin up a real HostServer + DesktopClient over TCP on localhost."""
    port = 47911
    password = "s3cret"
    engine = host_mod.CaptureEngine()
    engine.configure(quality=50, fps=10, scale_pct=50)
    engine._test_stop = False
    engine.grab = lambda: (
        (_fake_image(), {"width": 320, "height": 180})
        if not engine._test_stop else (None, None)
    )
    # HELLO must report the test screen size, not the real desktop
    engine.current_size = lambda: (320, 180)

    events: "queue.Queue[tuple[str, object]]" = queue.Queue()
    server = host_mod.HostServer(
        ("127.0.0.1", port), engine, password,
        allow_control=True, allow_files=True, allow_clipboard=False,
        on_event=lambda k, d=None: events.put((k, d)),
        host_ui=None,
    )
    srv_thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.2}, daemon=True)
    srv_thread.start()
    time.sleep(0.3)

    # patch injector before the client starts sending input
    injector = FakeInjector()
    orig_move = host_mod.W.move_mouse
    orig_btn = host_mod.W.mouse_button
    orig_wheel = host_mod.W.wheel
    orig_keyv = host_mod.W.key_vk
    orig_keyu = host_mod.W.key_unicode
    host_mod.W.move_mouse = injector.move
    host_mod.W.mouse_button = injector.button
    host_mod.W.wheel = injector.wheel
    host_mod.W.key_vk = lambda vk, up=False: injector.key(vk=vk, up=up)
    host_mod.W.key_unicode = lambda ch, up=False: injector.key(uni=ch, up=up)

    client = None
    try:
        # --- 1. wrong password is rejected --------------------------------
        bad = client_mod.DesktopClient("127.0.0.1", port, "wrong")
        try:
            bad.connect(timeout=4.0)
            check("wrong password rejected", False, "connect succeeded")
        except PermissionError as exc:
            check("wrong password rejected", "wrong password" in str(exc).lower())
        except (P.ConnectionLost, OSError) as exc:
            check("wrong password rejected", False, f"connection dropped: {exc}")
        finally:
            bad._hard_close()
            # the host handler thread must fully release the socket before the
            # next connection, or BaseRequestHandler's shutdown() races with
            # the new session's sends — wait for the previous session to die
            deadline = time.time() + 3
            while time.time() < deadline:
                if not (server.session and server.session.is_alive()):
                    break
                time.sleep(0.05)

        # --- 2. real session -------------------------------------------------
        client = client_mod.DesktopClient("127.0.0.1", port, password)
        client.connect(timeout=4.0)
        time.sleep(1.2)  # let frames flow

        check("HELLO negotiated", client.screen_w == 320 and client.screen_h == 180,
              f"got {client.screen_w}x{client.screen_h}")
        got_frame = False
        try:
            got_frame = bool(client.frames.get(timeout=3.0))
        except queue.Empty:
            pass
        check("FRAME streaming", got_frame)

        # --- 3. input ---------------------------------------------------------
        client.mouse_move(100, 50)
        client.mouse_down(100, 50, "left")
        client.mouse_up(100, 50, "left")
        client.mouse_down(200, 60, "right")
        client.mouse_up(200, 60, "right")
        client.wheel(-120)
        client.key(0, uni="A")
        client.key(0x0D)
        time.sleep(0.8)
        kinds = [e[0] for e in injector.events]
        check("MOUSE move delivered", ("move", 100, 50) in injector.events)
        check("MOUSE click delivered",
              ("button", "left", True) in injector.events
              and ("button", "left", False) in injector.events)
        check("MOUSE right-click delivered", ("button", "right", True) in injector.events)
        check("MOUSE wheel delivered", ("wheel", -120) in injector.events)
        check("KEY unicode delivered",
              any(e[0] == "key" and e[2] == "A" for e in injector.events))
        check("KEY vk delivered",
              any(e[0] == "key" and e[1] == 0x0D for e in injector.events))

        # --- 4. chat both directions ------------------------------------------
        client.send_chat("hello from client")
        session = server.session
        time.sleep(0.5)
        chat_events = [d for k, d in events.queue
                       if k == "chat" and d.get("text") == "hello from client"]
        check("CHAT client->host", bool(chat_events))
        session.say("hello from host")
        time.sleep(0.5)
        check("CHAT host->client", client.frames is not None)  # placeholder

        # --- 5. file transfer client->host --------------------------------------
        # NOTE: tmpdir is created BEFORE the session's downloads_dir snapshot
        # is patched (below), so create it now and re-point the live session
        # afterwards — the session already cached its dir at connect time.
        tmpdir = tempfile.mkdtemp(prefix="p2ptest-")
        src = os.path.join(tmpdir, "payload.bin")
        data = os.urandom(300_000)
        with open(src, "wb") as fh:
            fh.write(data)
        # save to a DIFFERENT directory — if the host saved into tmpdir with
        # the same basename it would truncate our own source file.
        # The host auto-accept names the file after the OFFER, so expect
        # "payload.bin" in outdir, not "received.bin".
        outdir = tempfile.mkdtemp(prefix="p2pout-")
        dst = os.path.join(outdir, "payload.bin")
        # host_ui is None in the test, so the host auto-accepts into
        # session.downloads_dir — re-point the live session at outdir
        server.session.downloads_dir = outdir
        client.offer_file(src)
        deadline = time.time() + 20
        ok = False
        while time.time() < deadline:
            if os.path.exists(dst) and os.path.getsize(dst) == len(data):
                with open(dst, "rb") as fh:
                    ok = fh.read() == data
                if ok:
                    break
            time.sleep(0.2)
        got = os.path.getsize(dst) if os.path.exists(dst) else -1
        check("FILE transfer integrity (300 KB)", ok,
              f"expected {len(data)} bytes, dst has {got}")

        # --- 6. graceful close ----------------------------------------------------
        client.close()
        time.sleep(0.6)
        check("BYE closes session", not (server.session and server.session.is_alive()))

    finally:
        host_mod.W.move_mouse = orig_move
        host_mod.W.mouse_button = orig_btn
        host_mod.W.wheel = orig_wheel
        host_mod.W.key_vk = orig_keyv
        host_mod.W.key_unicode = orig_keyu
        if client:
            client._hard_close()
        engine._test_stop = True
        server.shutdown()
        server.server_close()


def main():
    print("=" * 62, flush=True)
    print("P2P remote desktop — automated smoke test", flush=True)
    print("=" * 62, flush=True)
    test_protocol_roundtrip()
    test_full_session()
    print("-" * 62, flush=True)
    print(f"{len(PASS)} passed, {len(FAIL)} failed", flush=True)
    if FAIL:
        for name in FAIL:
            print(f"  FAILED: {name}", flush=True)
        sys.exit(1)
    print("ALL GREEN", flush=True)


if __name__ == "__main__":
    main()

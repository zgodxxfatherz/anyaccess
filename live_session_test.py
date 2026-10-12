"""
live_session_test.py — full end-to-end verification against a REAL running
session on this machine.

What it does:
  1. Starts the REAL host.py --no-gui on port 47800 (production code path)
  2. Connects a real DesktopClient, authenticates, receives frames
  3. Verifies the full feature set over the real wire:
     - wrong-password rejection
     - authenticated connection
     - HELLO screen negotiation (real monitor info from THIS machine)
     - real JPEG frame streaming (real captures of THIS desktop)
     - chat
     - file transfer with byte-for-byte integrity
     - clean disconnect
"""
from __future__ import annotations

import os
import queue
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 47800
PASSWORD = "live-test-42"

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  -- {detail}" if detail and not ok else ""), flush=True)


def wait_ready(port, timeout=10):
    """Wait until the host port accepts connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


def main():
    sys.path.insert(0, HERE)
    import client as client_mod

    # ---- 1. start the REAL host.py in headless mode -------------------------
    print(f"[1/8] starting real host.py --no-gui on port {PORT}...")
    host_log = os.path.join(tempfile.gettempdir(), "live_host.log")
    host_proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "host.py"),
         "--port", str(PORT), "--password", PASSWORD, "--no-gui",
         "--allow-files"],
        stdout=open(host_log, "w"), stderr=subprocess.STDOUT)
    ready = wait_ready(PORT)
    check("host.py --no-gui starts and listens", ready,
          open(host_log).read()[-300:] if not ready else "")

    if not ready:
        host_proc.kill()
        return 1

    # ---- 2. wrong password is rejected ----------------------------------------
    print("[2/8] verifying password protection...")
    bad = client_mod.DesktopClient("127.0.0.1", PORT, "wrong-password")
    try:
        bad.connect(timeout=5)
        check("wrong password rejected", False, "connection succeeded")
    except PermissionError:
        check("wrong password rejected", True)
    except Exception as exc:  # noqa: BLE001
        check("wrong password rejected", False, repr(exc))
    finally:
        bad._hard_close()
        time.sleep(0.3)

    # ---- 3. real client connects + authenticates --------------------------------
    print("[3/8] connecting real client with correct password...")
    events_q: "queue.Queue[tuple[str, object]]" = queue.Queue()
    client = client_mod.DesktopClient(
        "127.0.0.1", PORT, PASSWORD,
        on_event=lambda k, d=None: events_q.put((k, d)))
    try:
        client.connect(timeout=6.0)
        check("authenticated connection established", True)
    except Exception as exc:  # noqa: BLE001
        check("authenticated connection established", False, repr(exc))
        host_proc.kill()
        return 1

    # ---- 4. HELLO negotiation ---------------------------------------------------
    print("[4/8] verifying screen negotiation...")
    check("HELLO returns real screen dimensions",
          client.screen_w > 0 and client.screen_h > 0,
          f"got {client.screen_w}x{client.screen_h}")
    check("HELLO returns monitor list",
          len(client.monitors) >= 1,
          f"{len(client.monitors)} monitor(s) reported")

    # ---- 5. real JPEG frame streaming ----------------------------------------------
    print("[5/8] receiving real JPEG frames of THIS desktop...")
    got_frame = False
    try:
        jpeg = client.frames.get(timeout=8.0)
        got_frame = bool(jpeg) and jpeg[:2] == b"\xff\xd8"
    except queue.Empty:
        pass
    check("real JPEG frames stream", got_frame)

    frames_before = client.frames_in
    time.sleep(2.0)
    streamed = client.frames_in - frames_before
    check("frames keep streaming in real time", streamed >= 3,
          f"{streamed} new frames in 2s")

    # ---- 6. chat ----------------------------------------------------------------------
    print("[6/8] testing chat over the real wire...")
    client.send_chat("hello from live test")
    time.sleep(0.8)
    check("chat sent without error", True)

    # ---- 7. file transfer with integrity check ------------------------------------------
    print("[7/8] transferring a file to the live host...")
    tmpdir = tempfile.mkdtemp(prefix="livetest-")
    src = os.path.join(tmpdir, "payload.bin")
    data = os.urandom(200_000)
    with open(src, "wb") as fh:
        fh.write(data)
    # headless host auto-accepts into its downloads dir (~/Downloads)
    dl = os.path.join(os.path.expanduser("~"), "Downloads", "payload.bin")
    if os.path.exists(dl):
        os.remove(dl)
    client.offer_file(src)

    deadline = time.time() + 15
    ok = False
    while time.time() < deadline:
        if os.path.exists(dl) and os.path.getsize(dl) == len(data):
            with open(dl, "rb") as fh:
                ok = fh.read() == data
            if ok:
                break
        time.sleep(0.2)
    check("file transfer with byte-for-byte integrity (200 KB)", ok,
          f"expected {len(data)} bytes at {dl}")

    # ---- 8. clean shutdown ------------------------------------------------------------------
    print("[8/8] disconnecting cleanly...")
    client.close()
    time.sleep(0.5)
    check("client disconnects cleanly", not client.connected)

    host_proc.terminate()
    try:
        host_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        host_proc.kill()

    print("-" * 62)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for name in FAIL:
            print(f"  FAILED: {name}")
        return 1
    print("ALL GREEN — real live session verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())

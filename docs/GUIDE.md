# AnyAccess — Step-by-Step Guide

A peer-to-peer remote desktop for Windows. One PC shares its screen; the
other views and controls it — mouse, keyboard, chat, and file transfer —
over a single direct TCP connection. No account, no server in the middle,
no internet required (LAN works out of the box).

**Requirements:** Windows 10/11 · Python 3.10+ · `pip install pillow mss`

---

## The two apps

| App | Runs on | What it does |
|---|---|---|
| `host.py` | The PC you want to **control** | Captures the screen, injects your mouse/keyboard |
| `client.py` | The PC you control **from** | Shows the remote screen, forwards your input |

Both must be on the same network (LAN) — or reachable over the internet
with a port forward / VPN.

---

## Step 1 — Start the Host

Double-click **`start-host.bat`** (or run `python host.py`).

The window opens showing:
- **Your address** — e.g. `HOST_IP:47800` (this is what the viewer needs)
- **Port** — default `47800`
- **Password** — set one (recommended); leave blank for no auth
- **Checkboxes** — allow/deny remote control, file transfer, clipboard sync
- **Share** — pick one monitor or all combined

![Step 1 — Host window, offline and ready](docs/step1_host_offline.png)

If Windows asks to allow Python through the firewall, click **Allow** on
private networks.

---

## Step 2 — Start Sharing

Click the blue **Start sharing** button.

- The badge top-right turns **green**: `● sharing`
- The Activity log shows `Sharing on <ip>:47800 (password required)`
- Click **Copy** to put the address on your clipboard

![Step 2 — Host is sharing, green badge](docs/step2_host_sharing.png)

---

## Step 3 — Connect from the Viewer

On the other PC, double-click **`start-client.bat`** (or run
`python client.py`).

1. Paste the host's address into **Host** (e.g. `HOST_IP`)
2. Enter the same **Port** (`47800`)
3. Type the **Password** you set on the host
4. Click **Connect**

The badge turns green: `● connected` and the remote screen appears in the
canvas. Your mouse and keyboard now control the remote PC.

![Step 3 — Viewer connected, remote screen streaming](docs/step3_client_connected.png)

---

## Step 4 — You're In

Once connected:

**Remote control** — move your mouse over the canvas; click, drag,
right-click, scroll. Type on your keyboard; it goes to the remote PC.
Works with any keyboard layout, including Persian text.

**Chat** — type in the chat box at the bottom right and press Enter.
Messages appear on both sides.

**File transfer** — click **Send file to remote…**, pick a file. The host
chooses where to save it (in the GUI; headless mode saves to `~/Downloads`).

**Stream quality** — drag the Quality / FPS / Scale sliders (right panel)
to trade sharpness for speed. Changes apply instantly mid-session.

**Fullscreen** — press `F11` (or the Fullscreen button). `Esc` to exit.

### Viewer shortcuts

| Key | Action |
|---|---|
| `F11` | toggle fullscreen |
| `Esc` | leave fullscreen |
| `F5` | reconnect |
| `Ctrl+L` | connect / disconnect |

---

## Stopping

- On the viewer: click **Disconnect** (or close the window).
- On the host: click **Stop** (or close the window).

Only one viewer can connect at a time; a second connection is refused
while a session is live.

---

## How it works (under the hood)

Everything runs on **one TCP connection** with a length-prefixed frame
(`[type:u32][len:u32][payload]`), so screen frames, input events, chat,
file chunks, and clipboard sync interleave safely on the same socket.

```
host.py      captures screen (mss) -> JPEG -> streams frames
             injects mouse/keyboard via Win32 SendInput
client.py    reader thread demultiplexes the stream
             JPEG frames -> bounded queue -> Tk canvas
             input events -> wire -> host injects them
```

- **Backpressure by dropping:** if the link is slow, old frames are
  discarded so the view stays live instead of lagging behind.
- **Rate limiting:** mouse moves are capped at ~60 Hz so the wire never
  floods.
- **Keepalive:** PING every 2 s; the viewer shows round-trip latency.

See `README.md` for the full architecture, threading model, security
notes, and test suite documentation.

---

## Security — read this

- v1 authenticates with a **plaintext password over unencrypted TCP**.
  That is fine on a trusted LAN, but **do not expose it to the internet**
  without a VPN or SSH tunnel: the password and all traffic can be
  sniffed, and anyone who connects can control the PC.
- Roadmap: TLS on the connection, AnyDesk-style ID + rendezvous server
  for NAT traversal.

---

## Files

```
anyaccess/
├── host.py          # the shared machine (screen capture + input injection)
├── client.py        # the viewer (screen display + input forwarding)
├── protocol.py      # wire framing + message types (shared)
├── wininput.py      # Win32 SendInput injection + clipboard (shared)
├── uikit.py         # dark Tk theme, chat panel, settings (shared)
├── smoke_test.py        # protocol test suite (14 checks)
├── gui_drive_test.py    # GUI widget test suite (10 checks)
├── live_session_test.py # live session test suite (10 checks)
├── start-host.bat   # double-click: run the host
├── start-client.bat # double-click: run the viewer
├── run-tests.bat    # double-click: run the test suite
├── docs/            # this guide + step-by-step screenshots
└── README.md        # full technical documentation
```

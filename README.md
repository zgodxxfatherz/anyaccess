# AnyAccess

**A peer-to-peer remote desktop for Windows.** One machine shares its screen;
another views and controls it — mouse, keyboard, chat, and file transfer — over
a single direct TCP connection. No account, no cloud server in the middle, and
no internet required: it works on your local network out of the box.

Built with Python and Tkinter. If you can run Python on Windows, you can run
this.

---

## Why

Most remote-desktop tools route your session through someone else's server and
ask you to sign up for an account. AnyAccess takes the opposite approach — two
processes talking directly to each other, nothing in between. It's a small,
readable codebase you can audit in an afternoon, and a good reference for how
screen capture, input injection, and streaming actually work on Windows.

## Features

- **Live screen sharing** — real-time JPEG stream, with quality / FPS / scale
  controls you can adjust mid-session.
- **Remote control** — full mouse (move, click, drag, right-click, scroll) and
  keyboard, including non-Latin layouts such as Persian, via the Win32 API.
- **Multi-monitor support** — share a single display or the combined desktop,
  and switch on the fly.
- **Two-way chat** — talk while you work, right in the sidebar.
- **File transfer** — send any file to the remote machine; the host chooses
  where to save it.
- **Clipboard sync** — copy text on one side, paste it on the other.
- **Access control** — optional password, plus independent toggles for remote
  control, file transfer, and clipboard access.
- **One session at a time** — a second connection is refused while a session is
  live.

## Requirements

- Windows 10 or 11
- Python 3.10 or newer
- `pip install pillow mss`
- Tkinter (bundled with Python on Windows)

That's it. No native build step, no `pywin32`, no GUI framework to install.

## Getting started

You run two programs: the **host** (the machine you want to control) and the
**client** (the machine you control from). Both live in this repo.

**1. On the machine you want to control:**

```bash
python host.py
```

Set a password (recommended), choose what to allow, pick a monitor, then click
**Start sharing**. The app shows your address — something like `HOST_IP:47800`
— and **Copy** puts it on your clipboard. The first time you run it, Windows
may ask to allow Python through the firewall; click **Allow** on private
networks.

**2. On the other machine:**

```bash
python client.py
```

Paste the host's address, enter the port (default `47800`), type the password,
and click **Connect**. The remote screen appears, and your mouse and keyboard
now drive the remote machine.

### Viewer shortcuts

| Key | Action |
|---|---|
| `F11` | Toggle fullscreen |
| `Esc` | Leave fullscreen |
| `F5` | Reconnect |
| `Ctrl+L` | Connect / disconnect |

## How it works

Everything runs over **one TCP connection** using a simple length-prefixed
frame: `[type: uint32][length: uint32][payload]`. Screen frames, input events,
chat, file chunks, and clipboard sync all interleave on the same socket,
because every message is self-framed.

- **`host.py`** captures the screen with [`mss`](https://github.com/BoboTiG/python-mss),
  encodes JPEG frames, and streams them. Incoming events are queued and drained
  on the UI thread, so a slow client never blocks the host's interface.
- **`client.py`** runs a reader thread that demultiplexes the stream. Frames go
  through a small bounded queue — old frames are dropped so the view stays
  live — and everything else is handed to the Tk UI.
- **`wininput.py`** injects mouse and keyboard events through the Win32
  `SendInput` API, using virtual-desktop absolute coordinates so it works
  correctly across any monitor layout or DPI.
- **`protocol.py`** defines the wire format and message types shared by both
  sides.
- **`uikit.py`** holds the dark theme, settings, and a few reusable widgets.

### Project layout

```
host.py              # the machine being controlled (screen capture + input)
client.py            # the controlling machine (screen display + input forwarding)
protocol.py          # wire framing and message types (shared)
wininput.py          # Win32 SendInput injection + clipboard (shared)
uikit.py             # settings, dark theme, reusable widgets (shared)
smoke_test.py        # protocol test suite
gui_drive_test.py    # Tk GUI widget test suite
live_session_test.py # end-to-end live session test suite
start-host.bat       # launch the host (Windows)
start-client.bat     # launch the client (Windows)
run-tests.bat        # run the test suites (Windows)
docs/                # screenshots and a printable guide
```

## Command line

```bash
python host.py --port 5000 --password secret   # custom port and password
python host.py --no-gui                        # headless server
python host.py --no-gui --allow-files          # headless, accept file sends
python client.py --host HOST_IP --port 47800   # connect to a specific host
```

Settings — port, password, quality, FPS, scale, and permissions — persist in
`p2p_settings.json` next to the scripts.

## Security — please read

Version 1 authenticates with a **plaintext password over an unencrypted TCP
connection**. That's acceptable on a trusted local network, but you should
**not** expose it to the public internet without a VPN or SSH tunnel: the
password and all session traffic can be intercepted, and anyone who connects
can control the machine.

On the roadmap, but not in this release:

- TLS on the connection
- An ID + rendezvous server for NAT traversal (so it works without port
  forwarding)
- Per-session access tokens

Until then: use it on networks you trust, and set a password.

## Testing

Three test suites, all green:

```bash
python smoke_test.py         # protocol + wire-format checks
python gui_drive_test.py     # drives the real Tk widgets end to end
python live_session_test.py  # a real host + client session, real screen capture
```

The GUI suite drives the actual widget event wiring — the real button commands,
the real canvas bindings — rather than synthesizing OS-level clicks, which
Windows blocks between processes. The live suite starts a real host and
connects a real client, exercising the production code path end to end.

## Contributing

Contributions are welcome. Please open an issue to talk through bigger changes
before sending a pull request, keep the code readable, and make sure the test
suites pass.

## License

Released under the MIT License. See [LICENSE](LICENSE).

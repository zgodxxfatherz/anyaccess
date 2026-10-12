"""
protocol.py — shared wire protocol for the P2P remote-desktop app.

Everything rides ONE TCP connection. Every message is self-framed:

    [4-byte type: big-endian uint32][4-byte payload length][payload bytes]

Payload is UTF-8 JSON for control messages, raw bytes for FRAME / FILE_DATA.
Because every message is length-prefixed, any message type can safely
interleave with any other on the same socket.
"""
from __future__ import annotations

import json
import socket
import struct

# ---------------------------------------------------------------- message ids
HELLO        = 1    # host  -> client  screen/monitor info + flags
FRAME        = 2    # host  -> client  JPEG bytes
MOUSE        = 3    # client -> host   {"type":"move|down|up|wheel", ...}
KEY          = 4    # client -> host   {"vk":int, "uni":str, "type":"down|up"}
CHAT         = 5    # both             {"text": str}
QUALITY      = 6    # client -> host   {"quality": int, "fps": int}
FILE_OFFER   = 7    # both             {"name","size","id"}
FILE_ACCEPT  = 8    # both             {"id","ok":bool,"save_path":str,"why":str}
FILE_DATA    = 9    # both             raw chunk bytes (paired by stream order)
FILE_DONE    = 10   # both             {"id","ok":bool}
PING         = 11   # both             payload = b"<timestamp>" (echoed in PONG)
PONG         = 12   # both
AUTH         = 13   # client -> host   {"password": str}
AUTH_RESULT  = 14   # host  -> client  {"ok": bool, "why": str}
CTRL_STATE   = 15   # host  -> client  {"control": bool, "files": bool, "clipboard": bool}
CLIPBOARD    = 16   # both             {"text": str}
MONITOR      = 17   # client -> host   {"index": int}
BYE          = 18   # both             graceful close

HEADER = struct.Struct(">II")          # (type, payload_length)
MAX_PAYLOAD = 64 * 1024 * 1024         # 64 MB sanity cap


class ProtocolError(Exception):
    """Raised on malformed framing / absurd payload sizes."""


class ConnectionLost(Exception):
    """Raised when a send/recv fails because the socket went away."""


# ---------------------------------------------------------------- send / recv
def encode_payload(payload) -> bytes:
    if payload is None:
        return b""
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return json.dumps(payload).encode("utf-8")


def send_msg(sock: socket.socket, mtype: int, payload=b"") -> None:
    """Send one framed message. Raises ConnectionLost on socket failure."""
    data = encode_payload(payload)
    try:
        sock.sendall(HEADER.pack(mtype, len(data)) + data)
    except OSError as exc:
        raise ConnectionLost(f"send failed: {exc}") from exc


def recv_exact(sock: socket.socket, n: int):
    """Read exactly n bytes. Returns None on clean EOF."""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(min(65536, n - len(buf)))
        except OSError as exc:
            raise ConnectionLost(f"recv failed: {exc}") from exc
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def recv_msg(sock: socket.socket):
    """Read one framed message -> (type, payload). (None, None) on clean EOF."""
    hdr = recv_exact(sock, HEADER.size)
    if hdr is None:
        return None, None
    mtype, length = HEADER.unpack(hdr)
    if length > MAX_PAYLOAD:
        raise ProtocolError(f"payload too large: {length} bytes")
    payload = recv_exact(sock, length) if length else b""
    if payload is None:
        return None, None
    return mtype, payload


def send_json(sock: socket.socket, mtype: int, obj) -> None:
    send_msg(sock, mtype, json.dumps(obj).encode("utf-8"))


def recv_json(sock: socket.socket):
    """Read one message expected to be JSON -> (type, dict). (None, None) on EOF."""
    mtype, payload = recv_msg(sock)
    if mtype is None:
        return None, None
    return mtype, json.loads(payload.decode("utf-8"))


def tune_socket(sock: socket.socket) -> None:
    """Low-latency settings applied to every accepted/connected socket."""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass

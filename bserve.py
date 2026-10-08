#!/usr/bin/env python3
"""
bserve — BHTTP/1 file server.  See SPEC.md.

    ./bserve [-v] [-g] ROOT PORT
      -v  hexdump every frame received and sent to stderr
      -g  "grease": send an unknown frame type before every response, to
          prove the client obeys the MUST-skip rule (SPEC §5)

Track 1 of the pair.  Deliberately self-contained: the spec is the
only thing that crosses between this server and any client.
"""
import argparse
import email.utils
import itertools
import mimetypes
import os
import socket
import sys
import threading

PREFACE = b"BHTTP/1\n"
HDR_LEN = 8
MAX_PAYLOAD = 16384
MAX_FIELDS = 64

T_DATA, T_HEADERS, T_GOAWAY = 0x00, 0x01, 0x07
T_GREASE = 0x2A                    # not defined by v1: clients must skip it
F_END_STREAM = 0x01

E_PROTOCOL, E_FRAME_SIZE = 1, 2

TABLE = [None, ":method", ":path", ":authority", ":status", "user-agent",
         "accept", "server", "content-type", "content-length", "last-modified"]
INDEX = {name: i for i, name in enumerate(TABLE) if name}
NAME_CHARS = set(b"abcdefghijklmnopqrstuvwxyz0123456789-_.")
TYPE_NAMES = {T_DATA: "DATA", T_HEADERS: "HEADERS", T_GOAWAY: "GOAWAY"}

VERBOSE = GREASE = False
ROOT = ""
LOG_LOCK = threading.Lock()                         # keep one frame's lines together


def log(msg):
    with LOG_LOCK:
        print(msg, file=sys.stderr, flush=True)


def hexdump(data):
    lines = []
    for off in range(0, len(data), 16):
        row = data[off:off + 16]
        hexes = " ".join(f"{b:02x}" for b in row[:8])
        if len(row) > 8:
            hexes += "  " + " ".join(f"{b:02x}" for b in row[8:])
        text = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in row)
        lines.append(f"    {off:04x}  {hexes:<49} |{text}|")
    return "\n".join(lines)


class Malformed(Exception):
    """A header block or request that earns a 400 (a stream error)."""


# ---------- header blocks (SPEC §4) ---------------------------------------

def encode_block(fields):
    out = bytearray()
    for name, value in fields:
        v = value.encode()
        if name in INDEX:
            out.append(0x80 | INDEX[name])
        else:
            out += bytes([0x00, len(name)]) + name.encode()
        out += len(v).to_bytes(2, "big") + v
    return bytes(out)


def decode_block(p):
    fields, i, n = [], 0, len(p)
    while i < n:
        if len(fields) == MAX_FIELDS:
            raise Malformed("more than 64 fields")
        b = p[i]; i += 1
        if b & 0x80:
            idx = b & 0x7F
            if not 1 <= idx <= 10:
                raise Malformed(f"unknown static index {idx}")
            name = TABLE[idx]
        elif b == 0x00:
            if i >= n:
                raise Malformed("truncated literal name")
            ln = p[i]; i += 1
            raw = p[i:i + ln]
            if ln == 0 or len(raw) != ln:
                raise Malformed("bad literal name length")
            if any(c not in NAME_CHARS and not (c == 0x3A and k == 0) for k, c in enumerate(raw)):
                raise Malformed("bad character in field name")
            name = raw.decode(); i += ln
        else:
            raise Malformed(f"reserved name-ref octet 0x{b:02x}")
        if i + 2 > n:
            raise Malformed("truncated value length")
        vl = int.from_bytes(p[i:i + 2], "big"); i += 2
        val = p[i:i + vl]
        if len(val) != vl:
            raise Malformed("truncated value")
        if any(c in (0, 0x0D, 0x0A) for c in val):
            raise Malformed("NUL/CR/LF in value")
        fields.append((name, val.decode("latin-1")))
        i += vl
    return fields


# ---------- one connection ------------------------------------------------

class Conn:
    def __init__(self, sock, no):
        self.sock, self.no = sock, no

    def recv_exact(self, n):
        """Exactly n bytes, or fewer if the peer closed."""
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return bytes(buf)

    def show_frame(self, d, frame):
        """-v: one summary line, the decoded header block, and the raw bytes."""
        if not VERBOSE:
            return
        length, ftype, flags = int.from_bytes(frame[0:3], "big"), frame[3], frame[4]
        end = " END_STREAM" if flags & F_END_STREAM and ftype in (T_DATA, T_HEADERS) else ""
        lines = [f"[conn {self.no}] {d} {TYPE_NAMES.get(ftype, 'UNKNOWN')}(0x{ftype:02x}) "
                 f"len={length} flags=0x{flags:02x}{end} stream={int.from_bytes(frame[5:8], 'big')}"]
        if ftype == T_HEADERS and len(frame) > HDR_LEN:
            try:
                lines += [f"    {n}: {v}" for n, v in decode_block(frame[HDR_LEN:])]
            except Malformed as e:
                lines.append(f"    (malformed header block: {e})")
        lines.append(hexdump(frame))
        log("\n".join(lines))

    def send_frame(self, ftype, flags, sid, payload=b""):
        assert len(payload) <= MAX_PAYLOAD
        frame = len(payload).to_bytes(3, "big") + bytes([ftype, flags]) + sid.to_bytes(3, "big") + payload
        self.show_frame(">", frame)
        self.sock.sendall(frame)                     # one write per frame

    def goaway(self, code, last, why):
        self.send_frame(T_GOAWAY, 0, 0, bytes([code]) + last.to_bytes(3, "big") + why.encode()[:128])
        log(f"[conn {self.no}] GOAWAY code={code}: {why}")

    def serve(self):
        preface = self.recv_exact(8)
        if VERBOSE:
            log(f"[conn {self.no}] < PREFACE len={len(preface)}\n{hexdump(preface)}")
        if preface != PREFACE:
            self.goaway(E_PROTOCOL, 0, 'bad preface: expected "BHTTP/1\\n"')
            return
        last_sid = 0
        while True:
            h = self.recv_exact(HDR_LEN)
            if len(h) != HDR_LEN:
                return                               # clean close, or EOF inside a frame
            length, ftype, flags = int.from_bytes(h[0:3], "big"), h[3], h[4]
            sid = int.from_bytes(h[5:8], "big")

            if length > MAX_PAYLOAD:                 # framing is lost: connection error
                self.show_frame("<", h)
                self.goaway(E_FRAME_SIZE, last_sid, "frame length exceeds 16384")
                return
            payload = self.recv_exact(length)
            if len(payload) != length:
                return
            self.show_frame("<", h + payload)

            if ftype == T_HEADERS:
                if sid == 0 or sid <= last_sid:
                    self.goaway(E_PROTOCOL, last_sid, "stream id must be >0 and increasing")
                    return
                last_sid = sid
                self.handle_request(sid, payload)
            elif ftype == T_DATA:
                if sid == 0:
                    self.goaway(E_PROTOCOL, last_sid, "DATA on stream 0")
                    return
                # v1: request bodies are discarded
            elif ftype == T_GOAWAY:
                return                               # peer is leaving
            else:
                # SPEC §5: unknown type — payload already consumed, carry on.
                log(f"[conn {self.no}] skipped unknown frame type 0x{ftype:02x} ({length} bytes)")

    # ---------- requests and responses -------------------------------------

    def send_head(self, sid, status, ctype, clen, mtime=None, end=False):
        fields = [(":status", f"{status:03d}"), ("server", "bserve/1.0"),
                  ("content-type", ctype), ("content-length", str(clen))]
        if mtime is not None:
            fields.append(("last-modified", email.utils.formatdate(mtime, usegmt=True)))
        if GREASE:
            self.send_frame(T_GREASE, 0xFF, sid, b"v2 says hi")
        self.send_frame(T_HEADERS, F_END_STREAM if end else 0, sid, encode_block(fields))

    def reply_text(self, sid, status, body, head=False):
        b = body.encode()
        self.send_head(sid, status, "text/plain; charset=utf-8", len(b), end=head or not b)
        if b and not head:
            self.send_frame(T_DATA, F_END_STREAM, sid, b)

    def handle_request(self, sid, block):
        try:
            fields = decode_block(block)
            method = path = None
            seen_regular = False
            for name, value in fields:
                if name.startswith(":"):
                    if seen_regular:
                        raise Malformed("pseudo-header after regular field")
                    if name == ":method":
                        if method is not None:
                            raise Malformed("duplicate :method")
                        method = value
                    elif name == ":path":
                        if path is not None:
                            raise Malformed("duplicate :path")
                        path = value
                    elif name != ":authority":
                        raise Malformed("unknown or response pseudo-header in request")
                else:
                    seen_regular = True
            if method is None or path is None:
                raise Malformed("missing :method or :path")
            if not path.startswith("/"):
                raise Malformed(":path must start with /")
        except Malformed as e:
            log(f"[conn {self.no}] stream {sid} -> 400 ({e})")
            self.reply_text(sid, 400, f"400 bad request: {e}\n")
            return

        if method not in ("GET", "HEAD"):
            log(f"[conn {self.no}] stream {sid} {method} -> 405")
            self.reply_text(sid, 405, "405 method not allowed\n")
            return
        head = method == "HEAD"

        resolved = resolve(path)
        if resolved is None:
            log(f"[conn {self.no}] stream {sid} {method} {path} -> 404")
            self.reply_text(sid, 404, "404 not found\n", head)
            return

        with open(resolved, "rb") as f:
            st = os.fstat(f.fileno())
            size = st.st_size
            log(f"[conn {self.no}] stream {sid} {method} {path} -> 200 ({size} bytes)")
            ctype = mimetypes.guess_type(resolved)[0] or "application/octet-stream"
            if ctype.startswith("text/"):
                ctype += "; charset=utf-8"
            self.send_head(sid, 200, ctype, size, st.st_mtime, end=head or size == 0)
            if head or size == 0:
                return
            sent = 0
            while sent < size:
                chunk = f.read(MAX_PAYLOAD)
                if not chunk:                        # file shrank under us: end anyway
                    self.send_frame(T_DATA, F_END_STREAM, sid)
                    return
                sent += len(chunk)
                self.send_frame(T_DATA, F_END_STREAM if sent >= size else 0, sid, chunk)


def resolve(path):
    """Map a request path to a regular file under ROOT, or None (→ 404)."""
    rel = path.split("?", 1)[0].split("#", 1)[0]
    if ".." in rel.split("/") or "\x00" in rel:
        return None
    full = ROOT + rel
    if rel.endswith("/"):
        full += "index.html"
    elif os.path.isdir(full):
        full += "/index.html"
    real = os.path.realpath(full)                    # defend against symlink escapes
    if not real.startswith(ROOT + os.sep) or not os.path.isfile(real):
        return None
    return real


# ---------- main ----------------------------------------------------------

def handle(sock, no):
    try:
        Conn(sock, no).serve()
    except (ConnectionError, OSError):
        pass
    finally:
        log(f"[conn {no}] closed")
        sock.close()


def main():
    global VERBOSE, GREASE, ROOT
    ap = argparse.ArgumentParser(prog="bserve", description="BHTTP/1 file server")
    ap.add_argument("-v", action="store_true", help="hexdump every frame")
    ap.add_argument("-g", action="store_true", help="send an unknown frame type before responses")
    ap.add_argument("root")
    ap.add_argument("port", type=int)
    a = ap.parse_args()
    VERBOSE, GREASE = a.v, a.g
    ROOT = os.path.realpath(a.root)
    if not os.path.isdir(ROOT):
        sys.exit(f"bserve: {a.root}: not a directory")

    if socket.has_dualstack_ipv6():                  # ::1 and 127.0.0.1 alike
        ls = socket.create_server(("", a.port), family=socket.AF_INET6, dualstack_ipv6=True,
                                  reuse_port=False, backlog=64)
    else:
        ls = socket.create_server(("", a.port), backlog=64)
    log(f"bserve: serving {ROOT} on port {a.port} (BHTTP/1)")

    counter = itertools.count(1)
    try:
        while True:
            sock, _ = ls.accept()
            no = next(counter)
            log(f"[conn {no}] accepted")
            threading.Thread(target=handle, args=(sock, no), daemon=True).start()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

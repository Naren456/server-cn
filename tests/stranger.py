#!/usr/bin/env python3
"""
A "stranger" BHTTP/1 client written only from SPEC.md, sharing no code with
bserve.py.  It sends deliberately good and bad bytes and checks the replies.

    python3 tests/stranger.py PORT [--grease]
      --grease  the server was started with -g: expect unknown frame types in
                every response, and check they can be skipped
"""
import socket
import struct
import sys

PORT = int(sys.argv[1])
EXPECT_GREASE = "--grease" in sys.argv
TABLE = [None, ":method", ":path", ":authority", ":status", "user-agent",
         "accept", "server", "content-type", "content-length", "last-modified"]
DATA, HEADERS, GOAWAY = 0x00, 0x01, 0x07
END = 0x01
failures = 0
unknown_seen = 0


def frame(ftype, flags, sid, payload=b""):
    n = len(payload)
    return bytes([n >> 16 & 255, n >> 8 & 255, n & 255, ftype, flags,
                  sid >> 16 & 255, sid >> 8 & 255, sid & 255]) + payload


def field(name, value):
    v = value.encode()
    ref = bytes([0x80 | TABLE.index(name)]) if name in TABLE else \
        bytes([0, len(name)]) + name.encode()
    return ref + struct.pack(">H", len(v)) + v


def request(sid, path, method="GET"):
    blk = field(":method", method) + field(":path", path) + field(":authority", f"localhost:{PORT}")
    return frame(HEADERS, END, sid, blk)


def recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise EOFError("server closed the connection")
        buf += chunk
    return buf


def read_frame(s):
    h = recv_exact(s, 8)
    n = h[0] << 16 | h[1] << 8 | h[2]
    return h[3], h[4], h[5] << 16 | h[6] << 8 | h[7], recv_exact(s, n)


def decode(blk):
    out, i = [], 0
    while i < len(blk):
        b = blk[i]; i += 1
        if b & 0x80:
            name = TABLE[b & 0x7F]
        else:
            ln = blk[i]; name = blk[i + 1:i + 1 + ln].decode(); i += 1 + ln
        vl = blk[i] << 8 | blk[i + 1]; i += 2
        out.append((name, blk[i:i + vl].decode())); i += vl
    return out


def response(s, sid):
    """Read one full response, skipping unknown frame types (SPEC §5)."""
    global unknown_seen
    status, body = None, b""
    while True:
        t, fl, fsid, p = read_frame(s)
        if t == GOAWAY:
            return ("GOAWAY", p[0], p[4:].decode())
        if t not in (DATA, HEADERS):
            unknown_seen += 1
            continue
        assert fsid == sid, f"response on stream {fsid}, expected {sid}"
        if t == HEADERS:
            status = int(dict(decode(p))[":status"])
        else:
            body += p
        if fl & END:
            return status, body


def connect():
    s = socket.create_connection(("127.0.0.1", PORT))
    s.sendall(b"BHTTP/1\n")
    return s


def check(name, cond, detail=""):
    global failures
    print(("  PASS " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))
    failures += 0 if cond else 1


# 1. plain GET
s = connect(); s.sendall(request(1, "/hello.txt"))
st, body = response(s, 1)
check("GET /hello.txt -> 200 + body", st == 200 and body == b"Hello, binary world!\n", f"{st} {body!r}")

if EXPECT_GREASE:
    check("server sent unknown frame types, skipped cleanly", unknown_seen > 0, f"{unknown_seen} seen")
    sys.exit(1 if failures else 0)

# 2. a large binary file arrives as several DATA frames and reassembles exactly
s.sendall(request(2, "/big.bin"))
st, body = response(s, 2)
check("100 kB binary over multiple DATA frames, byte-identical",
      st == 200 and body == open("www/big.bin", "rb").read(), f"{st} {len(body)} bytes")

# 3. malformed header block (reserved name-ref octet 0x42) -> 400, connection survives
s.sendall(frame(HEADERS, END, 3, b"\x42\x00\x00"))
st, body = response(s, 3)
check("malformed header block -> 400", st == 400, f"{st} {body!r}")
s.sendall(request(5, "/hello.txt"))
st, _ = response(s, 5)
check("same connection still usable after 400", st == 200, str(st))

# 4. unknown frame types (on stream 0 and on a live stream, odd flags) are skipped
s.sendall(frame(0x99, 0xFF, 0, b"future stuff") + frame(0x42, 0, 7, b"x" * 300) + request(7, "/"))
st, body = response(s, 7)
check("unknown frames skipped, then GET / -> 200 index.html", st == 200 and b"<html>" in body, str(st))

# 5. missing :path -> 400
s.sendall(frame(HEADERS, END, 9, field(":method", "GET")))
check("missing :path -> 400", response(s, 9)[0] == 400)

# 6. literal (non-indexed) names work; a custom header is fine
blk = bytes([0, 7]) + b":method" + b"\x00\x03GET" + field(":path", "/hello.txt") + field("x-trace", "abc")
s.sendall(frame(HEADERS, END, 11, blk))
check("literal-name encoding accepted -> 200", response(s, 11)[0] == 200)

# 7. path traversal -> 404, unknown file -> 404, POST -> 405, HEAD -> no body
for sid, path, meth, want in [(13, "/../../etc/passwd", "GET", 404), (15, "/nope.html", "GET", 404),
                              (17, "/hello.txt", "POST", 405)]:
    s.sendall(request(sid, path, meth))
    check(f"{meth} {path} -> {want}", response(s, sid)[0] == want)
s.sendall(request(19, "/hello.txt", "HEAD"))
st, body = response(s, 19)
check("HEAD -> 200, no DATA", st == 200 and body == b"")

# 8. connection error: non-increasing stream id -> GOAWAY
s.sendall(request(19, "/hello.txt"))
r = response(s, 19)
check("reused stream id -> GOAWAY PROTOCOL_ERROR", r[0] == "GOAWAY" and r[1] == 1, str(r))
s.close()

# 9. frame too large -> GOAWAY FRAME_SIZE_ERROR
s = connect(); s.sendall(bytes([0x01, 0x00, 0x00, 0x01, 0x01, 0, 0, 1]))   # Length = 65536
r = response(s, 1)
check("Length > 16384 -> GOAWAY FRAME_SIZE_ERROR", r[0] == "GOAWAY" and r[1] == 2, str(r))
s.close()

# 10. bad preface: what a web browser or curl sends
s = socket.create_connection(("127.0.0.1", PORT)); s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
r = response(s, 0)
check("HTTP/1.1 text (browser/curl) -> GOAWAY PROTOCOL_ERROR", r[0] == "GOAWAY" and r[1] == 1, str(r))
s.close()

sys.exit(1 if failures else 0)

# BHTTP/1: HTTP, in binary

Course project: two tracks and one protocol. **This is Track 1, the server.**
The client (Track 2) is my partner's. The only thing that crosses between us
is `SPEC.md`.

| File | What it is |
|------|------------|
| `SPEC.md` | **Hand-in 1.** The protocol, in two pages, for a stranger. |
| `bserve.py` | **Hand-in 2.** The server: `./bserve ./www 9000` (`bserve` is a symlink to it) |
| `HEXDUMP.md` | **Hand-in 3.** One complete request and response, annotated byte by byte. |
| `tests/stranger.py` | A test client written only from SPEC.md, never from `bserve.py`. It sends good and deliberately broken bytes. |
| `tests/run.sh` | Starts the server, runs the test client: 18 checks. |
| `www/` | Sample document root. |

## Build and run

Python 3.8+, standard library only, nothing to compile.

```bash
./bserve ./www 9000        # serve ./www on port 9000
./bserve -v ./www 9000     # also hexdump every frame in and out (stderr)
./bserve -g ./www 9000     # also send an unknown frame type before each response
./tests/run.sh             # run the tests (uses port 9137)
```

To check against the partner's client: `./bcurl -v localhost:9000/index.html`.
To check with no client at all: build the bytes by hand and pipe them through
`nc`, as `HEXDUMP.md` does.

## Design decisions, and why

**1. Frame header = 8 bytes: Length 24 / Type 8 / Flags 8 / Stream ID 24.**
HTTP/2 uses 24/8/8/31 plus 1 reserved bit (9 bytes). It has those widths for
these reasons:
* *24-bit length:* 16 bits (64 KiB) was judged too small to grow into, and
  32 bits is wasteful. With 24 bits, frame size can be raised later (via
  SETTINGS_MAX_FRAME_SIZE) while the default stays 16 KiB, so no single frame
  blocks a multiplexed connection for long.
* *8-bit type and 8-bit flags:* that is enough for every extension anyone
  has registered.
* *31-bit stream ID + R bit:* HTTP/2 multiplexes thousands of streams over a
  long-lived connection. 31 bits keeps the value positive in a signed 32-bit
  int, which matters in languages like Java with no unsigned types. The
  reserved bit was a hedge that was never used.

I kept HTTP/2's length, type, and flags. I cut the stream ID to 24 bits
because v1 is sequential: 16 M requests per connection is ample, and dropping
the never-used R bit makes the header exactly 8 bytes, a single 64-bit read.
I kept a stream ID at all, even without multiplexing, for three reasons:
responses name their request, GOAWAY can report "processed up to N", and a
v2 can multiplex without changing the header.

**2. Headers: HPACK's first two mechanisms, in an evening.**
* *Static table:* the ten names that v1 actually sends are each encoded as
  one byte, `0x80 | index`. Clients send `:method :path :authority
  user-agent accept`. The server sends `:status server content-type
  content-length last-modified`.
* *Literal names:* anything else is `0x00, len, name`. Every value is
  length-prefixed with 2 bytes.
* *Left out:* Huffman coding and the dynamic table. They save more bytes,
  but they add state that both sides must keep in sync, and that state is
  where HPACK's bugs live.

**3. The version-2 rule.** Length comes *before* Type and is bounded
(≤ 16384). Because of that, every receiver can skip any frame type it has
never heard of without understanding it. The server does this (it logs
"skipped unknown frame type …" and carries on), and with `-g` it sends unknown types of its own,
which keeps clients honest too. New features in v2 can therefore be new frame
types that v1 peers ignore.

**4. Stream errors vs connection errors.** A malformed header block is
still correctly *framed*: Length told us where it ends. So it costs only that
request (400), and the connection carries on. A bad Length or a bad preface
destroys the framing, so it costs the connection (GOAWAY, then close).

## Mapping to the brief

| Brief | Where |
|-------|-------|
| accept, read one binary frame, map path under a root | `Conn.serve`, `resolve` (rejects `..` and symlink escapes) |
| reply status, headers, bytes | `Conn.send_head` + DATA frames of ≤16 KiB |
| 404 if not there / 400 if malformed | `Conn.handle_request` (stream error, connection stays open) |
| keep the connection open | the frame loop in `Conn.serve` |
| unknown frame type MUST be skipped | SPEC §5; the final `else:` branch in `Conn.serve` |
| annotated hexdump | `bserve -v` → `HEXDUMP.md` |
# server-cn
# server-cn

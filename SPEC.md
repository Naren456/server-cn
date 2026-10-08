# BHTTP/1 — HTTP semantics in binary frames

Status: v1, complete. Audience: someone writing a client or a server who has
never seen ours. The key words MUST, MUST NOT, SHOULD and MAY are used as in
RFC 2119. All multi-octet integers are **unsigned, big-endian (network order)**.

## 1. Connection

1. The client opens one TCP connection and sends the 8-octet **preface**
   `42 48 54 54 50 2F 31 0A` (ASCII `BHTTP/1\n`). The server sends no preface.
2. A server that receives a different preface MUST send GOAWAY
   (PROTOCOL_ERROR) and close. This is how an HTTP/1.1 client talking to us by
   mistake fails fast and clearly.
3. After the preface, both sides send only frames (§2). The connection carries
   any number of request/response exchanges and stays open until a peer closes
   it or sends GOAWAY. A client SHOULD reuse it instead of opening another.

## 2. Frame header (8 octets, fixed)

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-----------------------------------------------+---------------+
|                  Length (24)                  |   Type (8)    |
+---------------+-----------------------------------------------+
|   Flags (8)   |                Stream ID (24)                 |
+---------------+-----------------------------------------------+
|                   Payload (Length octets) ...                 |
```

| Field     | Bits | Meaning |
|-----------|------|---------|
| Length    | 24   | Octets of payload that follow (header not counted). |
| Type      | 8    | Frame type (§3). |
| Flags     | 8    | Type-specific bits. Undefined bits MUST be sent as 0 and MUST be ignored on receipt. |
| Stream ID | 24   | 0 = the connection itself; 1..16 777 215 = one request/response exchange. |

**MAX_PAYLOAD = 16 384.** A sender MUST NOT send a frame whose Length exceeds
16 384. A receiver that reads a larger Length MUST treat it as a connection
error (FRAME_SIZE_ERROR). The limit applies to every type, known or not.

**Why 24 / 8 / 8 / 24** (HTTP/2 uses 24 / 8 / 8 / 1+31):
* *Length 24.* It is the same as HTTP/2's. A 16-bit length would cap frames
  at 64 KiB forever. With 24 bits, a v2 can raise MAX_PAYLOAD without changing
  the header. v1 still caps frames at 16 KiB, so a receiver needs one
  fixed-size buffer and a large body cannot hog the connection.
* *Type 8, Flags 8.* 256 types is plenty because unknown types are skipped
  (§5), which makes Type the extension point. Eight per-type flags cost one
  octet and we use one.
* *Stream ID 24, no reserved bit.* HTTP/2 needs 31 bits for very long-lived,
  heavily multiplexed connections, and its R bit was never used. We serve
  files sequentially. 16 M exchanges per connection is ample, and when a
  connection runs out, the client opens a new one. Dropping 8 bits makes the
  header exactly **8 octets**, a single 64-bit load. We keep a stream ID at
  all, even though v1 is sequential, so that every response names its
  request, GOAWAY can say what was processed, and a v2 can multiplex
  without a new header.

## 3. Frame types

| Type | Name    | Stream | Payload |
|------|---------|--------|---------|
| 0x00 | DATA    | ≠ 0    | Body octets. Any length 0..16 384. |
| 0x01 | HEADERS | ≠ 0    | One complete header block (§4). |
| 0x07 | GOAWAY  | 0      | Octet 0: error code. Octets 1–3: last stream ID processed. Octets 4…: optional UTF-8 reason. |
| other| —       | any    | Unknown. MUST be skipped (§5). |

Flag **END_STREAM = 0x01** (on DATA and HEADERS) means "the last frame of this
message". Error codes: 0 NO_ERROR, 1 PROTOCOL_ERROR, 2 FRAME_SIZE_ERROR,
3 INTERNAL_ERROR. Values above 3 MUST be treated as INTERNAL_ERROR.

## 4. Header block

A header block is a sequence of fields with no count and no terminator: the
frame's Length bounds it. Each field is a **name reference** followed by a
**value**.

```
name ref:  1iiiiiii                 indexed: i = static index 1..10
           00000000 LLLLLLLL name   literal: L = 1..255, then L octets
           (any other first octet is reserved → malformed)
value:     VVVVVVVV VVVVVVVV value  16-bit length, then that many octets
```

**Static table** (these are the ten names v1 implementations actually send):

| Idx | Byte | Name           | Idx | Byte | Name           |
|-----|------|----------------|-----|------|----------------|
| 1   | 0x81 | `:method`      | 6   | 0x86 | `accept`       |
| 2   | 0x82 | `:path`        | 7   | 0x87 | `server`       |
| 3   | 0x83 | `:authority`   | 8   | 0x88 | `content-type` |
| 4   | 0x84 | `:status`      | 9   | 0x89 | `content-length` |
| 5   | 0x85 | `user-agent`   | 10  | 0x8A | `last-modified`|

Rules: a sender SHOULD use the index when the name is in the table. Names are
lower-case: `a-z 0-9 - _ .`, plus a leading `:` for pseudo-headers. Values are
octets but MUST NOT contain NUL, CR or LF, so every message can still be
rewritten as HTTP/1.1. Numbers (`:status`, `content-length`) are ASCII decimal.
That makes them readable in a hexdump, and both are short. All pseudo-headers
MUST precede all regular fields. A block MAY hold at most 64 fields. This is
HPACK's static table plus its literal form, without the Huffman coding and
the dynamic table.

**Request:** exactly one `:method` and one `:path` (starting with `/`). It
SHOULD include `:authority` (host:port). No `:status`.
**Response:** exactly one `:status` (3 digits), and no request pseudo-headers.
It SHOULD include `content-length` and `content-type`.

## 5. Unknown things — the version-2 rule

**A receiver that meets a frame type it does not know MUST read and discard
exactly Length octets of payload and continue with the next frame. It MUST NOT
close the connection or send an error because of it.** Because the Length
always comes before the Type and is bounded by MAX_PAYLOAD, every receiver can
always skip any frame. Unknown flag bits are ignored as well. Unknown or
reserved *name-ref* octets inside a header block are *not* skippable (the
value boundary would be unknown), so they make the block malformed (§7).

## 6. Exchanges

* The client sends a request as **one HEADERS frame with END_STREAM set** on a
  new stream ID. IDs start anywhere ≥ 1 and MUST strictly increase on a
  connection. v1 requests have no body, and a server MUST discard any DATA
  frames a client sends.
* In v1, the client MUST wait for the response's END_STREAM before it sends the
  next request. A server MAY nonetheless read ahead, and it answers in order.
* The server replies on the same stream ID with **one HEADERS frame**, then
  zero or more **DATA frames**. END_STREAM is set on the last frame. If there
  is no body (HEAD, or zero length), END_STREAM goes on the HEADERS frame.
* Methods: `GET` sends the file. `HEAD` sends the same headers without DATA.
  Any other method gets 405.
* Server path mapping: drop anything from the first `?` or `#`, then append
  to the document root. A path ending in `/` (or naming a directory) gets
  `index.html`. A path with a `..` segment, or one that resolves outside the
  root, MUST be answered with 404. A missing file also gets 404.

## 7. Errors

| Situation | Kind | Action |
|-----------|------|--------|
| Header block malformed (bad name ref, truncated value, bad name chars, NUL/CR/LF in value, > 64 fields); request missing or duplicating `:method`/`:path`; `:path` not starting with `/`; pseudo after regular; unknown pseudo-header | **stream error** | server replies **400** on that stream; **connection stays open** |
| Bad preface; Length > 16 384; HEADERS/DATA on stream 0; request stream ID not increasing | **connection error** | send GOAWAY(code), close |
| EOF inside a frame | connection error | close |

A malformed header block leaves the framing intact (Length told us where it
ends), which is why it costs only one stream. A bad Length destroys the
framing, which is why it costs the whole connection.
A client MUST treat a HEADERS or DATA frame for a stream it did not request
as PROTOCOL_ERROR (unknown types are still just skipped). It SHOULD exit non-zero when `:status` is 4xx or 5xx.

## 8. Example (stream 1, `GET /hello.txt`)

```
42 48 54 54 50 2F 31 0A                      preface "BHTTP/1\n"
00 00 2F 01 01 00 00 01                      HEADERS len=47 END_STREAM stream=1
81 00 03 47 45 54                            :method = "GET"
82 00 0A 2F 68 65 6C 6C 6F 2E 74 78 74       :path   = "/hello.txt"  …
```
The fully annotated capture of one request and response is in `HEXDUMP.md`.

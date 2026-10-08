# Annotated capture: one complete request and response

The server is run with `-v`, which hexdumps every frame it receives and
sends. The request was built **by hand from SPEC.md**. No client program was
used: the bytes were written out with `printf` and sent with `nc`, which
proves that the spec alone is enough to talk to the server.

```
$ ./bserve -v ./www 9000
$ printf '\x42\x48\x54\x54\x50\x2f\x31\x0a\x00\x00\x2f\x01\x01\x00\x00\x01\x81\x00\x03\x47\x45\x54\x82\x00\x0a\x2f\x68\x65\x6c\x6c\x6f\x2e\x74\x78\x74\x83\x00\x0e\x6c\x6f\x63\x61\x6c\x68\x6f\x73\x74\x3a\x39\x30\x30\x30\x85\x00\x02\x6e\x63\x86\x00\x03\x2a\x2f\x2a' \
    | nc -q 1 localhost 9000 | xxd
```

`www/hello.txt` contains the 21 bytes `Hello, binary world!\n`. Every byte that
crossed the socket is shown below, in order, and nothing was left out.

---

## Client → server (63 bytes)

### Preface (8 bytes, §1)

```
42 48 54 54 50 2f 31 0a     "BHTTP/1\n" — magic and major version, sent once per connection
```

### Frame 1: HEADERS, the request (8 + 47 bytes)

```
00 00 2f                    Length    = 0x00002f = 47 payload bytes follow
01                          Type      = 0x01 HEADERS
01                          Flags     = 0x01 END_STREAM (requests have no body)
00 00 01                    Stream ID = 1 (first exchange on this connection)
```
Header block (47 bytes):
```
81                          name ref 1xxxxxxx → static index 1 = ":method"
00 03                       value length = 3
47 45 54                    "GET"

82                          static index 2 = ":path"
00 0a                       value length = 10
2f 68 65 6c 6c 6f 2e 74 78 74            "/hello.txt"

83                          static index 3 = ":authority"
00 0e                       value length = 14
6c 6f 63 61 6c 68 6f 73 74 3a 39 30 30 30   "localhost:9000"

85                          static index 5 = "user-agent"
00 02                       value length = 2
6e 63                       "nc"

86                          static index 6 = "accept"
00 03                       value length = 3
2a 2f 2a                    "*/*"
```
Check: 5 fields × (1 name byte + 2 length bytes) = 15, plus the values
3 + 10 + 14 + 2 + 3 = 32. 15 + 32 = **47** = `0x2f`. ✔

The server decodes this block (shown in its `-v` log), checks it has exactly
one `:method` and one `:path`, and maps `/hello.txt` to `www/hello.txt`.
It then logs `[conn 1] stream 1 GET /hello.txt -> 200 (21 bytes)`.

---

## Server → client (121 bytes)

### Frame 2: HEADERS, the response head (8 + 84 bytes)

```
00 00 54                    Length    = 0x54 = 84
01                          Type      = HEADERS
00                          Flags     = 0 — no END_STREAM: a body follows
00 00 01                    Stream ID = 1 — answers request 1
```
Header block (84 bytes):
```
84  00 03  32 30 30                         :status        = "200"
87  00 0a  62 73 65 72 76 65 2f 31 2e 30    server         = "bserve/1.0"
88  00 19  74 65 78 74 2f 70 6c 61 69 6e    content-type   = "text/plain; charset=utf-8"
           3b 20 63 68 61 72 73 65 74 3d    (0x19 = 25 bytes)
           75 74 66 2d 38
89  00 02  32 31                            content-length = "21"
8a  00 1d  54 68 75 2c 20 30 38 20 4f 63    last-modified  = "Thu, 08 Oct 2026 12:00:00 GMT"
           74 20 32 30 32 36 20 31 32 3a    (0x1d = 29 bytes)
           30 30 3a 30 30 20 47 4d 54
```
Check: 5 × 3 = 15, plus the values 3 + 10 + 25 + 2 + 29 = 69. 15 + 69 = **84** = `0x54`. ✔

### Frame 3: DATA, the body (8 + 21 bytes)

```
00 00 15                    Length    = 0x15 = 21 (equals content-length)
00                          Type      = 0x00 DATA
01                          Flags     = END_STREAM — this is the last frame of response 1
00 00 01                    Stream ID = 1
48 65 6c 6c 6f 2c 20 62 69 6e 61 72 79 20 77 6f 72 6c 64 21 0a
                            "Hello, binary world!\n"
```

After END_STREAM, the response is complete. The server **does not close**.
It goes back to reading the next 8-byte frame header and is ready for a
request on stream ≥ 2. Here, `nc` hangs up a second later, so the server reads
a clean EOF between frames and logs `[conn 1] closed`.

---

## What the bytes show

* **Indexing pays for itself.** Every field name in this exchange cost
  1 byte. The response head is 92 bytes on the wire; the same head in
  HTTP/1.1 text is 146 bytes.
* **The server never scans.** Each read is "8 bytes, then exactly Length
  bytes". It never looks for `\r\n\r\n` and never guesses where a value ends.
* **Unknown frames.** With `bserve -g`, an extra frame
  `00 00 0a 2a ff 00 00 01 "v2 says hi"` (type 0x2A, all flags set) goes out
  before frame 2. A conforming client reads its 10 payload bytes, discards
  them, and sees the exchange above unchanged. That is the §5 rule.

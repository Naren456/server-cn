#!/usr/bin/env bash
# Tests for the server (track 1).  The client side is played by
# tests/stranger.py, written from SPEC.md only — never from bserve's code.
#
#   ./tests/run.sh            (uses port 9137; override with PORT=xxxx)
set -u
cd "$(dirname "$0")/.."
PORT=${PORT:-9137}
LOG=$(mktemp); fail=0
head -c 100000 /dev/urandom > www/big.bin       # > 6 DATA frames

python3 bserve.py www "$PORT" 2>"$LOG" & SRV=$!
trap 'kill $SRV 2>/dev/null; rm -f "$LOG" www/big.bin' EXIT
sleep 0.5

echo "== spec-only client against bserve"
python3 tests/stranger.py "$PORT" || fail=1

# stranger.py makes 11 valid requests over its first connection (then a 12th
# with a reused stream id, which earns GOAWAY), plus 2 throwaway connections
# for the other GOAWAY tests: the server must have kept the first one open.
conns=$(grep -c accepted "$LOG")
streams=$(grep -c "\[conn 1\] stream" "$LOG")
[ "$conns" -eq 3 ] && [ "$streams" -eq 11 ] \
    && echo "  PASS keep-alive: $streams requests served on connection 1" \
    || { echo "  FAIL keep-alive: $conns connections, $streams streams on conn 1"; fail=1; }
grep -q "skipped unknown frame type 0x99" "$LOG" \
    && echo "  PASS server logged skipping unknown frame type 0x99" \
    || { echo "  FAIL unknown frame not skipped"; fail=1; }

echo "== bserve -g (sends unknown frame types; clients must skip them)"
kill $SRV; wait $SRV 2>/dev/null
python3 bserve.py -g www "$PORT" 2>"$LOG" & SRV=$!
sleep 0.5
python3 tests/stranger.py "$PORT" --grease || fail=1

echo
[ $fail -eq 0 ] && echo "ALL TESTS PASSED" || echo "SOME TESTS FAILED"
exit $fail

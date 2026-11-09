#!/usr/bin/env bash
# Smoke test: start the exporter and check auth, the host allowlist and clean
# shutdown. Needs the deps from requirements.txt and no real LW3 device.
#
#   scripts/smoke-test.sh            # uses `python` on PATH
#   PYTHON=.venv/bin/python scripts/smoke-test.sh
set -u

PYTHON="${PYTHON:-python}"
PORT="${PORT:-19879}"
BASE="http://127.0.0.1:${PORT}"
cd "$(dirname "$0")/.."

fail=0
check() { # check <description> <expected-http-code> <curl args...>
    local desc="$1" want="$2"
    shift 2
    local got
    got=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$@")
    if [ "$got" = "$want" ]; then
        echo "ok   $desc ($got)"
    else
        echo "FAIL $desc: expected $want, got $got"
        fail=1
    fi
}

# config.example.yaml ships with the allowlist enabled, so it exercises that too.
LW3_AUTH_USERNAME=prom LW3_AUTH_PASSWORD=s3cret \
    "$PYTHON" -m lw3_exporter -c config.example.yaml --host 127.0.0.1 --port "$PORT" \
    >/tmp/lw3-smoke.log 2>&1 &
pid=$!

for _ in $(seq 1 30); do
    curl -s -m 1 -o /dev/null "$BASE/healthz" && break
    sleep 0.5
done

check "healthz is open"                200 "$BASE/healthz"
check "metrics without auth"           401 "$BASE/metrics"
check "metrics with wrong password"    401 -u prom:wrong "$BASE/metrics"
check "metrics with auth"              200 -u prom:s3cret "$BASE/metrics"
check "probe without auth"             401 "$BASE/probe?host=matrix1.ugent.be"
check "probe host outside allowlist"   403 -u prom:s3cret "$BASE/probe?host=evil.example.com"
check "probe lookalike host"           403 -u prom:s3cret "$BASE/probe?host=matrix1.ugent.be.evil.com"
check "probe port outside allowlist"   403 -u prom:s3cret "$BASE/probe?host=matrix1.ugent.be&port=22"

# SIGTERM must stop the server promptly (regression: shutdown() deadlock).
kill -TERM "$pid"
for _ in $(seq 1 10); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
done
if kill -0 "$pid" 2>/dev/null; then
    echo "FAIL SIGTERM: server still running after 5s"
    kill -9 "$pid"
    fail=1
else
    echo "ok   SIGTERM shuts down cleanly"
fi

[ "$fail" = 0 ] || { echo "--- server log"; cat /tmp/lw3-smoke.log; }
exit "$fail"

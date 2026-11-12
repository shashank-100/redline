#!/usr/bin/env bash
# Checks your deliverable against the contract in the README.
# Usage: ./validate.sh
# Grading runs this same shape of check first.
set -u

PORT="${PORT:-8641}"
BOOT_LIMIT=180
REVIEW_LIMIT=300
SRV_PID=""
WORK=""

cleanup() {
  [ -n "$SRV_PID" ] && { kill "$SRV_PID" 2>/dev/null; wait "$SRV_PID" 2>/dev/null; }
  [ -n "$WORK" ] && rm -rf "$WORK"
}
trap cleanup EXIT
fail() { printf "FAIL: %b\n" "$1"; exit 1; }

[ -f ./start ] || fail "./start not found at the package root"
[ -x ./start ] || fail "./start is not executable (chmod +x start)"
[ -f ./playbook/msa_playbook.json ] || fail "./playbook/msa_playbook.json is missing - it is an input, leave it in place"

# Validate a COPY with installed-dependency directories removed, because that is
# what grading actually runs. `litmus submit` never uploads node_modules, venv,
# .venv, env, build, dist, target, .gradle, .idea or __pycache__ — they are large
# and machine-specific — and the grading sandbox installs nothing for you. So a
# ./start that assumes its dependencies are already on disk passes here in your
# working directory and then fails at grading with a module-not-found error.
# Checking the copy is the only way this script can tell you that in advance.
#
# Your ./start is what installs them: the README asks for a start script "that
# handles whatever your system needs to build and run". `npm ci`, `pip install
# -r requirements.txt`, `go mod download` — whatever your stack needs, before it
# serves.
STRIP_DIRS="node_modules __pycache__ venv .venv env target build dist .gradle .idea"
WORK="$(mktemp -d)"
TAR_EXCLUDES=""
for d in $STRIP_DIRS; do TAR_EXCLUDES="$TAR_EXCLUDES --exclude=$d"; done
# shellcheck disable=SC2086
tar -cf - $TAR_EXCLUDES . 2>/dev/null | (cd "$WORK" && tar -xf -) \
  || fail "could not stage a clean copy for validation"
for d in $STRIP_DIRS; do
  if [ -e "./$d" ]; then
    echo "NOTE: ./$d exists here but is NOT uploaded — validating without it."
  fi
done
cd "$WORK" || fail "could not enter the staging copy"
# Say where, so a stack trace pointing at a temp path is not a mystery.
echo "Validating a clean copy of what gets uploaded, staged at: $WORK"
[ -x ./start ] || chmod +x ./start 2>/dev/null

# Load .env if present, then warn (not fail) if AI credentials are missing.
[ -f ./.env ] && set -a && . ./.env && set +a
[ -n "${LITMUS_AI_BASE_URL:-}" ] && [ -n "${LITMUS_AI_API_KEY:-}" ] || \
  echo "WARN: LITMUS_AI_BASE_URL / LITMUS_AI_API_KEY not set - copy them from your assessment page (see README)"

echo "Starting: PORT=$PORT ./start   (boot limit ${BOOT_LIMIT}s)"
PORT="$PORT" ./start &
SRV_PID=$!

START=$(date +%s)
READY=""
while [ $(( $(date +%s) - START )) -lt "$BOOT_LIMIT" ]; do
  kill -0 "$SRV_PID" 2>/dev/null || fail "./start exited before becoming ready.\n      If it ran fine outside this script, the cause is almost always a missing\n      dependency: grading gets your source, not your installed packages, so\n      ./start has to install them itself before it serves."
  if curl -sf -o /dev/null "http://127.0.0.1:$PORT/"; then READY=1; break; fi
  sleep 2
done
[ -n "$READY" ] || fail "GET / did not return 200 within ${BOOT_LIMIT}s"
echo "OK: ready in $(( $(date +%s) - START ))s"

DRAFT="$(ls contracts/*.txt 2>/dev/null | xargs -I{} sh -c 'wc -c "{}"' | sort -n | head -1 | awk '{print $2}')"
[ -n "$DRAFT" ] || fail "no sample drafts found under ./contracts/"
echo "POST /api/review ($DRAFT, limit ${REVIEW_LIMIT}s)"
RESP="$(mktemp)"
python3 - "$DRAFT" <<'EOF' > /tmp/redline_smoke_body.json
import json, sys
print(json.dumps({"contract": open(sys.argv[1]).read()}))
EOF
curl -sf --max-time "$REVIEW_LIMIT" -X POST "http://127.0.0.1:$PORT/api/review" \
  -H 'Content-Type: application/json' \
  --data @/tmp/redline_smoke_body.json \
  -o "$RESP" || fail "POST /api/review failed or exceeded ${REVIEW_LIMIT}s"

# The review format is yours (see README) - this only checks one came back.
[ -s "$RESP" ] || fail "POST /api/review returned an empty response - the review is the product"
echo "OK: review returned ($(wc -c < "$RESP" | tr -d ' ') bytes). First lines:"
head -c 400 "$RESP"; echo
grep -qiE "accepted|fallback|not.accepted|not.applicable" "$RESP" || \
  echo "WARN: no status vocabulary (accepted/fallback/not accepted/not applicable) found in the review - see the README"
grep -qiE "rule" "$RESP" || \
  echo "WARN: the review never mentions a playbook rule - every rule must have a status that cites it"

# The largest draft is the one that finds request-body limits (many
# frameworks default to ~100KB and 413 on a 300KB+ draft) and slow pipelines.
BIG="$(ls contracts/*.txt 2>/dev/null | xargs -I{} sh -c 'wc -c "{}"' | sort -n | tail -1 | awk '{print $2}')"
echo "POST /api/review ($BIG - the largest draft; limit ${REVIEW_LIMIT}s)"
python3 -c 'import json,sys; print(json.dumps({"contract": open(sys.argv[1]).read()}))' "$BIG" \
  > /tmp/redline_big_body.json
RESP2="$(mktemp)"
curl -sf --max-time "$REVIEW_LIMIT" -X POST "http://127.0.0.1:$PORT/api/review" \
  -H 'Content-Type: application/json' \
  --data @/tmp/redline_big_body.json \
  -o "$RESP2" || fail "POST /api/review failed on the largest draft.\n      If the small draft worked, the usual causes are a request-body size\n      limit in your framework (raise it well past the largest draft) or a\n      pipeline that cannot finish a large document inside ${REVIEW_LIMIT}s."
[ -s "$RESP2" ] || fail "empty review for the largest draft"
echo "OK: largest draft reviewed ($(wc -c < "$RESP2" | tr -d ' ') bytes)."

kill "$SRV_PID" 2>/dev/null
echo "OK: contract satisfied."

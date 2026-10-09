#!/usr/bin/env bash
# launch.sh — start the whole osaka-fasthaks stack.
#
#   1. Hashi server   (mobile-app/server, WebSocket on :8080)
#   2. reachy-mini-intermediator (:8043, publishes to Hashi)
#   3. Expo app       (mobile-app, Metro dev server)
#
# The only required env var is SHISA_API_KEY, in reachy/.env, shisa/.env or
# mobile-app/server/.env (any one is enough).
#
# Usage:
#   ./launch.sh --robot        # the demo: Reachy Mini mic, speaker and head
#   ./launch.sh                # no robot: laptop mic and laptop speaker
#   ./launch.sh --demo         # Hashi server in DEMO=true mode
#   ./launch.sh --skip app     # don't start Expo (e.g. phone already running)
#   ./launch.sh --skip reachy  # just Hashi + Expo
#   ./launch.sh --skip server  # reachy + Expo only (no publish)
#
# Seats are as seen from the robot; override with PARTICIPANT_A / PARTICIPANT_B,
# e.g. PARTICIPANT_A="Ben:en:left" PARTICIPANT_B="Aiko:ja:right" ./launch.sh --robot
#
# Everything runs in the foreground; Ctrl-C stops it all.

set -euo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"

# ---- defaults ---------------------------------------------------------------
HASHI_URL="ws://localhost:8080"
REACHY_PORT="${REACHY_PORT:-8043}"   # 8042 is taken by the Reachy Mini Control desktop app
ROBOT_HOST="${ROBOT_HOST:-reachy-mini.local}"
ROBOT=false
DEMO="${DEMO:-false}"
SKIP=""
PARTICIPANT_A="${PARTICIPANT_A:-Ben:en:left}"
PARTICIPANT_B="${PARTICIPANT_B:-Aiko:ja:right}"

while [ $# -gt 0 ]; do
  case "$1" in
    --robot)  ROBOT=true ;;
    --demo)   DEMO="true" ;;
    --skip)   SKIP="${SKIP} $2"; shift ;;
    *)        echo "unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

has_skip() { case " $SKIP " in *" $1 "*) return 0;; *) return 1;; esac; }

# ---- checks ----------------------------------------------------------------
ENV_FILE=""
for f in mobile-app/server/.env reachy/.env shisa/.env; do
  if grep -q '^SHISA_API_KEY=..' "$f" 2>/dev/null; then ENV_FILE="$ROOT/$f"; break; fi
done
if [ -z "$ENV_FILE" ]; then
  echo "ERROR: SHISA_API_KEY not set. Put SHISA_API_KEY=... in reachy/.env" >&2
  exit 1
fi

command -v node >/dev/null || { echo "node (>=22) not found" >&2; exit 1; }
if ! has_skip reachy; then
  REACHY_BIN="$ROOT/reachy/.venv/bin/reachy-mini-intermediator"
  [ -x "$REACHY_BIN" ] || REACHY_BIN="$(command -v reachy-mini-intermediator || true)"
  [ -n "$REACHY_BIN" ] \
    || { echo "reachy-mini-intermediator not installed. Run:" >&2
        echo "  cd reachy && python -m venv .venv && .venv/bin/pip install -e '.[all]'" >&2
        exit 1; }
  if $ROBOT; then
    curl -s -m5 "http://$ROBOT_HOST:8000/api/daemon/status" | grep -q '"state":"running"' \
      || { echo "Robot daemon backend not running; starting it..."
           curl -s -m30 -X POST "http://$ROBOT_HOST:8000/api/daemon/start?wake_up=true" >/dev/null || true
           sleep 5; }
  fi
fi

PIDS=()
cleanup() {
  echo ""
  echo "Stopping..."
  [ ${#PIDS[@]} -gt 0 ] && kill "${PIDS[@]}" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ---- 1. Hashi server (:8080) ------------------------------------------------
if ! has_skip server; then
  echo "==> [1/3] Hashi server on :8080 (DEMO=$DEMO)"
  ( cd mobile-app
    [ -d node_modules ] || npm install
    PORT=8080 DEMO="$DEMO" exec node --env-file="$ENV_FILE" server/index.ts ) &
  PIDS+=($!)
  sleep 1
else
  echo "==> [1/3] Hashi server skipped"
fi

# ---- 2. reachy-mini-intermediator (:8043, publishes to Hashi) ----------------
if ! has_skip reachy; then
  if $ROBOT; then
    SOURCE_ARGS=(--source reachy --robot-host "$ROBOT_HOST" --gestures)
  else
    SOURCE_ARGS=(--source mic --speak local)
  fi
  PUBLISH_ARGS=()
  has_skip server || PUBLISH_ARGS=(--publish "$HASHI_URL")
  echo "==> [2/3] reachy-mini-intermediator on :$REACHY_PORT (${SOURCE_ARGS[*]})"
  ( cd reachy
    exec "$REACHY_BIN" --port "$REACHY_PORT" "${SOURCE_ARGS[@]}" \
      --a "$PARTICIPANT_A" --b "$PARTICIPANT_B" "${PUBLISH_ARGS[@]}" ) &
  PIDS+=($!)
else
  echo "==> [2/3] reachy-mini-intermediator skipped"
fi

# ---- 3. Expo app ------------------------------------------------------------
if ! has_skip app; then
  echo "==> [3/3] Expo app (Metro) — scan the QR code with Expo Go"
  ( cd mobile-app
    [ -d node_modules ] || npm install
    exec npm start -- --port "${EXPO_PORT:-8081}" ) &
  PIDS+=($!)
else
  echo "==> [3/3] Expo app skipped"
fi

echo ""
echo "Stack up:"
echo "  Hashi WebSocket   ws://localhost:8080  (phone: use a 'Phone address' line above)"
echo "  reachy server     http://localhost:$REACHY_PORT/api/state"
echo "Ctrl-C stops everything."

wait

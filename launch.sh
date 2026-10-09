#!/usr/bin/env bash
# launch.sh — start the whole osaka-fasthaks stack.
#
#   1. Hashi server   (mobile-app/server, WebSocket on :8080)
#   2. reachy-mini-intermediator (:8042, publishes to Hashi)
#   3. Expo app       (mobile-app, Metro dev server)
#
# The only required env var is SHISA_API_KEY. Put it in reachy/.env,
# shisa/.env, or mobile-app/server/.env (any one is enough — reachy's
# config loader falls back across all three).
#
# Usage:
#   ./launch.sh                # everything, robot-free (mic/file source)
#   ./launch.sh --robot        # use the real Reachy Mini as audio source
#   ./launch.sh --demo         # Hashi server in DEMO=true mode (no robot needed)
#   ./launch.sh --skip app     # don't start Expo (e.g. phone already running)
#   ./launch.sh --skip reachy  # just Hashi + Expo
#   ./launch.sh --skip server  # reachy + Expo only (no publish)
#
# Everything runs in the foreground; Ctrl-C stops it all.

set -euo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"

# ---- defaults ---------------------------------------------------------------
HASHI_URL="ws://localhost:8080"
REACHY_SOURCE="mic"          # mic | reachy | file:<wav>
DEMO="${DEMO:-false}"
SKIP=""
PARTICIPANT_A="${PARTICIPANT_A:-Aiko:ja:left}"
PARTICIPANT_B="${PARTICIPANT_B:-Ben:en:right}"

while [ $# -gt 0 ]; do
  case "$1" in
    --robot)  REACHY_SOURCE="reachy" ;;
    --demo)   DEMO="true" ;;
    --skip)   SKIP="${SKIP} $2"; shift ;;
    *)        echo "unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

has_skip() { case " $SKIP " in *" $1 "*) return 0;; *) return 1;; esac; }

# ---- checks ----------------------------------------------------------------
if ! has_skip server; then
  if ! grep -rq 'SHISA_API_KEY=..' reachy/.env shisa/.env mobile-app/server/.env 2>/dev/null; then
    echo "ERROR: SHISA_API_KEY not set. Copy a .env.example to .env and fill it in:" >&2
    echo "  cp mobile-app/server/.env.example mobile-app/server/.env" >&2
    exit 1
  fi
fi

command -v node >/dev/null || { echo "node (>=22) not found" >&2; exit 1; }
if ! has_skip reachy; then
  command -v reachy-mini-intermediator >/dev/null \
    || { echo "reachy-mini-intermediator not installed. Run:" >&2
        echo "  cd reachy && pip install -e .[all]" >&2
        exit 1; }
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
    PORT="${PORT:-8080}" DEMO="$DEMO" \
      node --env-file=server/.env server/index.ts ) &
  PIDS+=($!)
  sleep 1
else
  echo "==> [1/3] Hashi server skipped"
fi

# ---- 2. reachy-mini-intermediator (:8042, publishes to Hashi) ----------------
if ! has_skip reachy; then
  echo "==> [2/3] reachy-mini-intermediator on :8042 (source: $REACHY_SOURCE)"
  if has_skip server; then
    reachy-mini-intermediator \
      --source "$REACHY_SOURCE" \
      --a "$PARTICIPANT_A" --b "$PARTICIPANT_B" &
  else
    reachy-mini-intermediator \
      --source "$REACHY_SOURCE" \
      --a "$PARTICIPANT_A" --b "$PARTICIPANT_B" \
      --publish "$HASHI_URL" \
      --publish-users "a=person-1,b=person-2" &
  fi
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
echo "  Hashi WebSocket   ws://localhost:8080"
echo "  reachy server     http://localhost:8042  (GET /api/state, /healthz)"
echo "  reachy WS client  reachy/examples/listen.py"
echo "Ctrl-C stops everything."

wait
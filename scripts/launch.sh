#!/bin/sh
# Launch FM27 (Steam, macOS) with the FM27Real bridge injected, and the world
# engine running alongside it.
#
# Steam must be running. SteamAppId tells the game it was started for Steam,
# so it does not hand the launch back to Steam (which would drop the bridge).
# Ownership is still checked by the running Steam client.
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
BRIDGE="$REPO/build/libfm27real.dylib"
GAME="${FM27_APP:-$HOME/Library/Application Support/Steam/steamapps/common/Football Manager 27/fm.app}"
DATA="$HOME/Library/Application Support/Sports Interactive/Football Manager 27/FM27Real"
APP_ID=4530370

[ -f "$BRIDGE" ] || { echo "bridge not built: run make -C \"$REPO/bridge\"" >&2; exit 1; }
pgrep -x steam_osx >/dev/null || { echo "start Steam first" >&2; exit 1; }

# World engine: start it unless one is already listening.
ENGINE_PID=""
if ! curl -s -o /dev/null --max-time 1 http://127.0.0.1:27271/; then
    mkdir -p "$DATA"
    (cd "$REPO" && exec python3 -m engine) >>"$DATA/engine.log" 2>&1 &
    ENGINE_PID=$!
    echo "engine started (log: $DATA/engine.log)"
fi
stop_engine() { [ -n "$ENGINE_PID" ] && kill "$ENGINE_PID" 2>/dev/null || true; }
trap stop_engine EXIT INT TERM

cd "$GAME/Contents/MacOS"
SteamAppId=$APP_ID SteamGameId=$APP_ID DYLD_INSERT_LIBRARIES="$BRIDGE" "./Football Manager 27"

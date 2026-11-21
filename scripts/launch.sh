#!/bin/sh
# Launch FM27 (Steam, macOS) with the FM27Real bridge injected.
# Steam must be running. SteamAppId tells the game it was started for Steam,
# so it does not hand the launch back to Steam (which would drop the bridge).
# Ownership is still checked by the running Steam client.
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
BRIDGE="$REPO/build/libfm27real.dylib"
GAME="${FM27_APP:-$HOME/Library/Application Support/Steam/steamapps/common/Football Manager 27/fm.app}"
APP_ID=4530370

[ -f "$BRIDGE" ] || { echo "bridge not built: run make -C \"$REPO/bridge\"" >&2; exit 1; }
pgrep -x steam_osx >/dev/null || { echo "start Steam first" >&2; exit 1; }

cd "$GAME/Contents/MacOS"
SteamAppId=$APP_ID SteamGameId=$APP_ID DYLD_INSERT_LIBRARIES="$BRIDGE" exec "./Football Manager 27"

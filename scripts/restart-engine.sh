#!/bin/sh
# Restart only the FM27Real world engine (e.g. after updating it), without
# restarting the game. The in-game panel reconnects by itself.
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$HOME/Library/Application Support/Sports Interactive/Football Manager 27/FM27Real"
pkill -f "python3 -m engine" 2>/dev/null && sleep 1
mkdir -p "$DATA"
(cd "$REPO" && nohup python3 -m engine >>"$DATA/engine.log" 2>&1 &)
sleep 1
curl -s -o /dev/null --max-time 2 http://127.0.0.1:27271/ && echo "engine restarted" || echo "engine did not start; see $DATA/engine.log"

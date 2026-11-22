#!/bin/sh
# Moved: see MacOS/restart-engine.sh.
exec "$(cd "$(dirname "$0")/.." && pwd)/MacOS/restart-engine.sh" "$@"

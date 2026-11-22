#!/bin/sh
# Moved: the macOS launcher lives in MacOS/launch.sh.
exec "$(cd "$(dirname "$0")/.." && pwd)/MacOS/launch.sh" "$@"

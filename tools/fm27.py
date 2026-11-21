#!/usr/bin/env python3
"""Command-line access to the bridge. See engine/bridge.py for the commands."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine.bridge import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv))

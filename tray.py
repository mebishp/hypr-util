#!/usr/bin/env python3
"""Entry point -- see hyprutil/ui/tray.py for the actual implementation."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hyprutil.ui.tray import main

if __name__ == "__main__":
    main()

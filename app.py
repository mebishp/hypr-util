#!/usr/bin/env python3
"""Entry point -- see hyprutil/ui/app.py for the actual implementation."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hyprutil.ui.app import main

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Deprecated single-file entry point kept for backwards compatibility.

The implementation now lives in the ``terminal_monitor`` package.  Running
``python3 terminal_monitor.py`` keeps working for one release cycle; prefer
``python3 -m terminal_monitor`` or the installed ``terminal-monitor`` script.
"""

from __future__ import annotations

import sys

from terminal_monitor import *  # noqa: F403
from terminal_monitor import main

if __name__ == "__main__":
    sys.exit(main())

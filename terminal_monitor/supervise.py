"""Packaged autonomous-supervision entry point.

Equivalent to the repository-root ``supervisor.py`` convenience script, but
installed with the package so ``pip install`` users get it too.  Relaunches
use ``python -m terminal_monitor`` so the saved launch metadata no longer
depends on a checkout-local file path.
"""

from __future__ import annotations

import sys
from dataclasses import replace

from .cli import build_parser, config_from_args
from .monitor import TerminalMonitor


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] != "supervise":
        args = ["supervise", *args]
    if "--prohibition" not in args:
        args.extend(["--prohibition", "Do not publish to npm."])

    parsed = build_parser().parse_args(args)
    config = config_from_args(parsed)
    launch = [sys.executable, "-m", "terminal_monitor", *args]
    if "--state-dir" not in launch:
        launch.extend(["--state-dir", config.state_dir])
    config = replace(config, launch_command=tuple(launch))
    return TerminalMonitor(config).run()


if __name__ == "__main__":
    sys.exit(main())

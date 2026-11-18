"""Packaged autonomous-supervision entry point.

Equivalent to the repository-root ``supervisor.py`` convenience script, but
installed with the package so ``pip install`` users get it too.  Relaunches
use ``python -m terminal_monitor`` so the saved launch metadata no longer
depends on a checkout-local file path.
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace

from .cli import _has_cli_flag, _require_supported_python, build_parser, config_from_args
from .config import DEFAULT_STATE_DIR, validate_managed_config
from .monitor import TerminalMonitor
from .state import enable_debug_log


def main(argv: list[str] | None = None) -> int:
    if not _require_supported_python():
        return 2
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] != "supervise":
        args = ["supervise", *args]
    if "--prohibition" not in args:
        args.extend(["--prohibition", "Do not publish to npm."])

    parsed = build_parser().parse_args(args)
    debug_log_path = getattr(parsed, "debug_log", None)
    if debug_log_path:
        enable_debug_log(debug_log_path)
    elif os.environ.get("TERMINAL_MONITOR_DEBUG", "").strip() not in ("", "0"):
        enable_debug_log(os.path.join(DEFAULT_STATE_DIR, "debug.log"))

    config = config_from_args(parsed)
    if not debug_log_path and config.debug_log_path:
        enable_debug_log(config.debug_log_path)
    if str(config.backend).lower() in ("pty", "managed", "managed-pty"):
        try:
            validate_managed_config(config.backend, config.agent_command)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    launch = [sys.executable, "-m", "terminal_monitor", *args]
    if not _has_cli_flag(launch, "--state-dir"):
        launch.extend(["--state-dir", config.state_dir])
    if not _has_cli_flag(launch, "--project-dir", "-d"):
        launch.extend(["--project-dir", config.project_dir])
    config = replace(config, launch_command=tuple(launch))
    return TerminalMonitor(config).run()


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Convenience supervisor script for running AI Agent Terminal Monitor in autonomous supervision mode."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))

import terminal_monitor


def main() -> int:
    args = [
        "supervise",
        "--process", "opencode",
        "--profile", "opencode",
        "--auto-allow-permissions",
        "--prohibition", "Do not publish to npm.",
        "--status-json", "/tmp/terminal-monitor/status.json",
    ]
    if len(sys.argv) > 1:
        args = sys.argv[1:]
        if args[0] != "supervise":
            args = ["supervise", *args]
        if "--prohibition" not in args:
            args.extend(["--prohibition", "Do not publish to npm."])

    parser = terminal_monitor.build_parser()
    parsed_args = parser.parse_args(args)
    config = terminal_monitor.config_from_args(parsed_args)
    monitor = terminal_monitor.TerminalMonitor(config)
    return monitor.run()


if __name__ == "__main__":
    sys.exit(main())

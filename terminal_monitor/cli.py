"""Command-line interface: argument parsing, config assembly, and entrypoint."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import __version__
from .backends import get_backend, validate_web_port
from .config import MonitorConfig, discover_config_file, generate_starter_config, load_config_file
from .github import (
    _parent_pid,
    build_restart_command,
    collect_final_evidence,
    evaluate_final_state,
    merge_pull_request,
    persist_restart_event,
    write_final_report,
)
from .gitinfo import resolve_project_state_dir
from .monitor import TerminalMonitor
from .processes import _children_pids, interrupt_process_tree
from .profiles import list_profiles
from .safety import UNSAFE_PHRASES, PolicyEnvelope
from .state import StateFileError, TaskState, json_safe
from .status import read_status_snapshot, render_status_dashboard, resume_monitor, stop_monitor


def build_parser() -> argparse.ArgumentParser:
    """Build comprehensive CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="terminal_monitor",
        description="Monitor and safely nudge AI CLI coding agents running in Terminal.app, iTerm2, or tmux.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # init config subcommand
    init_parser = subparsers.add_parser("init", help="Generate a starter configuration file")
    init_parser.add_argument("--format", choices=["json", "toml"], default="json", help="Configuration format (default: json)")
    init_parser.add_argument("-o", "--output", help="Output file path (default: .terminal-monitor.<format>)")

    # list profiles subcommand
    subparsers.add_parser("profiles", help="List built-in and discovered agent profiles")

    status_parser = subparsers.add_parser("status", help="Show a live colored monitor dashboard")
    status_parser.add_argument("--state-dir", default=None, help="Monitor state directory")
    status_parser.add_argument("--project-dir", "-d", default=None, help="Project directory for repository details")
    status_parser.add_argument("--json", action="store_true", dest="status_json", help="Print machine-readable JSON")
    status_parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    status_parser.add_argument("--watch", action="store_true", help="Refresh the dashboard continuously")
    status_parser.add_argument("--interval", type=float, default=2.0, help="Refresh interval for --watch")

    stop_parser = subparsers.add_parser("stop", help="Stop the monitor without interrupting the agent")
    stop_parser.add_argument("--state-dir", default=None, help="Monitor state directory")
    stop_parser.add_argument("--reason", default="cli_stop", help="Reason recorded for the stop")

    resume_parser = subparsers.add_parser("resume", help="Resume a monitor from saved launch metadata")
    resume_parser.add_argument("--state-dir", default=None, help="Monitor state directory")
    resume_parser.add_argument("--project-dir", "-d", default=None, help="Project directory for the monitor")

    send_parser = subparsers.add_parser("send", help="Send an explicit instruction to the selected agent terminal")
    send_parser.add_argument("text", help="Instruction text to send")
    _add_monitor_args(send_parser)

    interrupt_parser = subparsers.add_parser("interrupt-child", help="Interrupt only a verified child command")
    interrupt_parser.add_argument("--pid", type=int, required=True, help="Child process ID to interrupt")
    _add_monitor_args(interrupt_parser)

    restart_parser = subparsers.add_parser("restart-agent", help="Restart an agent using saved task state")
    restart_parser.add_argument("--continue-session", action="store_true", help="Pass the saved session identifier")
    restart_parser.add_argument("--agent-command", nargs=argparse.REMAINDER, help="Explicit agent command and arguments")
    _add_monitor_args(restart_parser)

    verify_parser = subparsers.add_parser("verify-final-state", help="Verify PR, branch, CI, release, and npm safety invariants")
    verify_parser.add_argument("--pr", type=int, help="Pull Request number (defaults to saved state/current branch)")
    _add_monitor_args(verify_parser)

    merge_parser = subparsers.add_parser("merge-pr", help="Merge a PR only after the exact-head green-check gate")
    merge_parser.add_argument("--pr", type=int, required=True, help="Pull Request number")
    merge_parser.add_argument("--head", required=False, help="Expected full 40-character PR head SHA (defaults to saved state)")
    _add_monitor_args(merge_parser)

    # supervise subcommand
    supervise_parser = subparsers.add_parser("supervise", help="Run autonomous supervision daemon")
    _add_monitor_args(supervise_parser)
    supervise_parser.add_argument("--status-json", dest="status_json_path", help="Path to write real-time status JSON")

    # Main monitor arguments
    _add_monitor_args(parser)
    parser.add_argument("--status-json", dest="status_json_path", help="Path to write real-time status JSON")

    return parser


def _add_monitor_args(parser: argparse.ArgumentParser) -> None:
    """Add standard monitor flags to a parser with None defaults so config files take effect."""
    parser.add_argument("--process", "-p", default=None, help="Agent process name to track (default: opencode)")
    parser.add_argument("--profile", default=None, help="Agent profile to use (e.g. claude, opencode, aider, goose)")
    parser.add_argument("--title", "-t", default=None, help="Window title substring filter")
    parser.add_argument("--continue-text", "-c", default=None, help="Text sent on idle or nudge")
    parser.add_argument("--continue-file", "-f", default=None, help="Path to file whose content is sent on idle")
    parser.add_argument("--poll-seconds", type=float, default=None, help="Loop interval (default: 3.0s)")
    parser.add_argument("--idle-seconds", type=float, default=None, help="Seconds before idle trigger (default: 15.0s)")
    parser.add_argument("--cooldown-seconds", type=float, default=None, help="Cooldown after sending (default: 20.0s)")
    parser.add_argument("--gone-seconds", type=float, default=None, help="Seconds before process gone (default: 25.0s)")
    parser.add_argument("--max-sends", type=int, default=None, help="Maximum sends before exit (default: 100)")
    parser.add_argument("--auto-allow-permissions", "-a", action="store_true", default=None, help="Auto-allow safe permission prompts")
    parser.add_argument("--supervise", "-S", action="store_true", default=None, help="Enable autonomous supervisor mode")
    parser.add_argument("--no-smart-nudges", action="store_true", help="Disable git-aware context smart nudges")
    parser.add_argument("--no-mode-switch", action="store_true", help="Disable automatic Plan->Build mode switching")
    parser.add_argument("--no-completion-check", action="store_true", help="Disable completion state auto-detection")
    parser.add_argument("--backend", "-b", choices=["auto", "terminal", "iterm2", "tmux"], default=None, help="Terminal backend")
    parser.add_argument("--project-dir", "-d", default=None, help="Project directory for config discovery and git status")
    parser.add_argument("--config", default=None, help="Explicit configuration file path")
    parser.add_argument("--state-dir", default=None, help="Directory for state/logs (default: /tmp/terminal-monitor)")
    parser.add_argument("--unsafe-phrase", action="append", dest="unsafe_phrases", help="Add custom unsafe phrases")
    parser.add_argument("--objective", default=None, help="Permanent task objective included with every nudge")
    parser.add_argument("--prohibition", action="append", dest="prohibitions", help="Permanent instruction that dynamic nudges cannot override")
    parser.add_argument("--task-id", default=None, help="Durable external task identifier")
    parser.add_argument("--required-outcome", default=None, help="Required final outcome (default: merged)")
    parser.add_argument("--allow-npm-publish", action="store_true", default=None, help="Explicitly allow npm publication (default: prohibited)")
    parser.add_argument("--session-id", default=None, help="Agent session identifier for robust selection/restart")
    parser.add_argument("--expected-branch", default=None, help="Pause supervision if the repository branch differs")
    parser.add_argument("--protected-branch", action="append", dest="protected_branches", help="Branch that must never be dirty during supervision")
    parser.add_argument("--report-path", default=None, help="Path for the structured final report JSON")
    parser.add_argument("--attempt-history-limit", type=int, default=None, help="Maximum persisted attempt/decision records")
    parser.add_argument("--no-loop-guard", action="store_true", help="Disable monitored-agent loop protection")
    parser.add_argument("--loop-repeat-limit", type=int, default=None, help="Repeated expensive command episodes allowed without progress")
    parser.add_argument("--queued-attempt-seconds", type=float, default=None, help="Seconds before a visibly queued message requires attention")
    parser.add_argument("--allow-history-rewrite", action="store_true", default=None, help="Allow monitored Git history-rewrite commands")
    parser.add_argument("--no-web-ui", action="store_true", help="Disable the local live web command center")
    parser.add_argument("--web-port", type=int, default=None, help="Preferred localhost port for the live web command center")
    parser.add_argument("--no-web-open", action="store_true", help="Start the web command center without opening a browser")
    parser.add_argument("--no-desktop-notifications", action="store_true", help="Disable native desktop notifications")
    parser.add_argument("--webhook-url", default=None, help="Webhook URL for event dispatch (Slack, Discord, custom)")
    parser.add_argument("--loop-interrupt-wait-seconds", type=float, default=None, help="Seconds to wait for a loop child tree to stop")
    parser.add_argument("--once", action="store_true", default=False, help="Inspect status once and exit")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Simulate actions without sending keystrokes")


def config_from_args(args: argparse.Namespace) -> MonitorConfig:
    """Build MonitorConfig merging defaults, discovered config file, and CLI flags."""
    project_dir = getattr(args, "project_dir", None) or "."
    file_cfg: dict[str, Any] = {}
    if getattr(args, "config", None):
        file_cfg = load_config_file(args.config)
    elif project_dir:
        discovered = discover_config_file(project_dir)
        if discovered:
            file_cfg = load_config_file(discovered)

    continue_text = getattr(args, "continue_text", None) if getattr(args, "continue_text", None) is not None else file_cfg.get("continue_text", "")
    if getattr(args, "continue_file", None):
        continue_path = Path(args.continue_file).resolve()
        if continue_path.is_file():
            continue_text = continue_path.read_text(encoding="utf-8").strip()

    is_supervise = bool(getattr(args, "supervise", False) or getattr(args, "command", "") == "supervise" or file_cfg.get("supervise", False))
    auto_allow = bool(getattr(args, "auto_allow_permissions", None) if getattr(args, "auto_allow_permissions", None) is not None else (is_supervise or file_cfg.get("auto_allow_permissions", False)))
    smart_nudges = not getattr(args, "no_smart_nudges", False) and bool(is_supervise or file_cfg.get("smart_nudges", True))
    auto_switch = not getattr(args, "no_mode_switch", False) and bool(is_supervise or file_cfg.get("auto_switch_modes", True))
    completion_check = not getattr(args, "no_completion_check", False) and bool(is_supervise or file_cfg.get("completion_check", True))

    process = getattr(args, "process", None) or file_cfg.get("process", "opencode")
    profile = getattr(args, "profile", None) or file_cfg.get("profile", process)

    def _val(arg_val: Any, cfg_key: str, default_val: Any) -> Any:
        if arg_val is not None:
            return arg_val
        return file_cfg.get(cfg_key, default_val)

    def _string_values(value: Any, default: list[str] | tuple[str, ...] = ()) -> list[str]:
        if value is None:
            return list(default)
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value]
        return list(default)

    cli_unsafe = getattr(args, "unsafe_phrases", None) or []
    file_unsafe = _string_values(file_cfg.get("unsafe_phrases", list(UNSAFE_PHRASES)), UNSAFE_PHRASES)
    merged_unsafe = list(dict.fromkeys([*file_unsafe, *cli_unsafe]))
    cli_prohibitions = getattr(args, "prohibitions", None) or []
    file_prohibitions = _string_values(file_cfg.get("prohibitions", []))
    merged_prohibitions = list(dict.fromkeys([*file_prohibitions, *cli_prohibitions]))

    raw_state_dir = str(_val(getattr(args, "state_dir", None), "state_dir", "/tmp/terminal-monitor"))
    resolved_state_dir = resolve_project_state_dir(raw_state_dir, str(project_dir)) if raw_state_dir == "/tmp/terminal-monitor" else raw_state_dir

    return MonitorConfig(
        process=process,
        profile=profile,
        title=_val(getattr(args, "title", None), "title", None),
        continue_text=continue_text,
        continue_file=getattr(args, "continue_file", None),
        poll_seconds=float(_val(getattr(args, "poll_seconds", None), "poll_seconds", 3.0)),
        idle_seconds=float(_val(getattr(args, "idle_seconds", None), "idle_seconds", 15.0)),
        cooldown_seconds=float(_val(getattr(args, "cooldown_seconds", None), "cooldown_seconds", 20.0)),
        gone_seconds=float(_val(getattr(args, "gone_seconds", None), "gone_seconds", 25.0)),
        max_sends=int(_val(getattr(args, "max_sends", None), "max_sends", 100)),
        auto_allow_permissions=auto_allow,
        once=bool(getattr(args, "once", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
        state_dir=resolved_state_dir,
        backend=str(_val(getattr(args, "backend", None), "backend", "auto")),
        project_dir=str(project_dir),
        unsafe_phrases=merged_unsafe,
        custom_profiles=file_cfg.get("custom_profiles", {}),
        supervise=is_supervise,
        auto_switch_modes=auto_switch,
        smart_nudges=smart_nudges,
        completion_check=completion_check,
        status_json_path=getattr(args, "status_json_path", None) or file_cfg.get("status_json_path"),
        objective=str(_val(getattr(args, "objective", None), "objective", "")),
        prohibitions=merged_prohibitions,
        task_id=str(_val(getattr(args, "task_id", None), "task_id", "")),
        required_outcome=str(_val(getattr(args, "required_outcome", None), "required_outcome", "merged")),
        npm_publish_allowed=bool(_val(getattr(args, "allow_npm_publish", None), "npm_publish_allowed", False)),
        session_id=str(_val(getattr(args, "session_id", None), "session_id", "")),
        expected_branch=str(_val(getattr(args, "expected_branch", None), "expected_branch", "")),
        protected_branches=tuple(_string_values(_val(getattr(args, "protected_branches", None), "protected_branches", ["main", "master"]))),
        report_path=str(_val(getattr(args, "report_path", None), "report_path", "")) or None,
        attempt_history_limit=max(1, int(_val(getattr(args, "attempt_history_limit", None), "attempt_history_limit", 100))),
        loop_guard=not getattr(args, "no_loop_guard", False) and bool(file_cfg.get("loop_guard", True)),
        loop_repeat_limit=max(2, int(_val(getattr(args, "loop_repeat_limit", None), "loop_repeat_limit", 3))),
        queued_attempt_seconds=max(0.0, float(_val(getattr(args, "queued_attempt_seconds", None), "queued_attempt_seconds", 45.0))),
        allow_history_rewrite=bool(_val(getattr(args, "allow_history_rewrite", None), "allow_history_rewrite", False)),
        web_ui=not getattr(args, "no_web_ui", False) and bool(file_cfg.get("web_ui", True)),
        web_port=validate_web_port(int(_val(getattr(args, "web_port", None), "web_port", 8765))),
        web_open_browser=not getattr(args, "no_web_open", False) and bool(file_cfg.get("web_open_browser", True)),
        loop_interrupt_wait_seconds=max(0.0, float(_val(getattr(args, "loop_interrupt_wait_seconds", None), "loop_interrupt_wait_seconds", 2.0))),
        desktop_notifications=not getattr(args, "no_desktop_notifications", False) and bool(file_cfg.get("desktop_notifications", True)),
        webhook_url=str(_val(getattr(args, "webhook_url", None), "webhook_url", "")),
    )


def main() -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args()

    # Handle init subcommand
    if args.command == "init":
        fmt = getattr(args, "format", "json")
        out_path = getattr(args, "output", None) or f".terminal-monitor.{fmt}"
        content = generate_starter_config(fmt)
        Path(out_path).write_text(content + "\n", encoding="utf-8")
        print(f"Created configuration template: {out_path}")
        return 0

    # Handle profiles subcommand
    if args.command == "profiles":
        print("Available Agent Profiles:")
        for name, desc in list_profiles().items():
            print(f"  • {name:<12} - {desc}")
        return 0

    config = config_from_args(args)
    if config.supervise:
        launch = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
        if "--state-dir" not in launch:
            launch.extend(["--state-dir", config.state_dir])
        if "--project-dir" not in launch and "-d" not in launch:
            launch.extend(["--project-dir", config.project_dir])
        for prohibition in config.prohibitions:
            if prohibition not in launch:
                launch.extend(["--prohibition", prohibition])
        config = replace(config, launch_command=tuple(launch))

    if args.command == "status":
        refresh = max(0.25, float(getattr(args, "interval", 2.0)))
        color = not bool(getattr(args, "no_color", False)) and sys.stdout.isatty()
        try:
            while True:
                snapshot = read_status_snapshot(config.state_dir, config.project_dir)
                if getattr(args, "status_json", False):
                    print(json.dumps(json_safe(snapshot), indent=2, sort_keys=True))
                else:
                    if getattr(args, "watch", False) and color:
                        print("\033[2J\033[H", end="")
                    print(render_status_dashboard(snapshot, color=color))
                if not getattr(args, "watch", False):
                    break
                time.sleep(refresh)
        except KeyboardInterrupt:
            return 130
        return 0

    if args.command == "stop":
        result = stop_monitor(config.state_dir, reason=str(getattr(args, "reason", "cli_stop")))
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 2

    if args.command == "resume":
        result = resume_monitor(config.state_dir, project_dir=config.project_dir)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 2

    if args.command == "send":
        policy = PolicyEnvelope(config.objective, tuple(config.prohibitions))
        allowed, reason = policy.authorize_action(
            args.text,
            unsafe_phrases=config.unsafe_phrases,
            npm_publish_allowed=config.npm_publish_allowed,
        )
        if not allowed:
            print(f"POLICY_BLOCKED: {reason}", file=sys.stderr)
            return 3
        if config.dry_run:
            print(json.dumps({"dry_run": True, "action": "send", "payload": args.text, "policy": reason}, indent=2))
            return 0
        backend = get_backend(config.backend)
        ok, detail = backend.send(config.process, config.title, args.text)
        print(detail)
        return 0 if ok else 1

    if args.command == "interrupt-child":
        backend = get_backend(config.backend)
        roots = set(backend.get_pids(config.process))
        ok = interrupt_process_tree(roots, args.pid, parent_of=_parent_pid, children_of=_children_pids)
        print("INTERRUPTED_TREE" if ok else "REFUSED_NOT_VERIFIED_DESCENDANT")
        return 0 if ok else 2

    if args.command == "restart-agent":
        state = TaskState.load(Path(config.state_dir, "task-state.json"))
        try:
            command = build_restart_command(config, state, args.agent_command, args.continue_session)
        except StateFileError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if config.dry_run:
            print(json.dumps({"dry_run": True, "action": "restart-agent", "command": command}, indent=2))
            return 0
        state = persist_restart_event(Path(config.state_dir, "task-state.json"), state, command)
        process = subprocess.Popen(command, cwd=config.project_dir, start_new_session=True)
        print(f"RESTARTED pid={process.pid}")
        return 0

    if args.command == "merge-pr":
        state = TaskState.load(Path(config.state_dir, "task-state.json"))
        expected_head = args.head or str(state.pr.get("head", ""))
        if not expected_head:
            print("An expected full PR head SHA is required (--head or saved state).", file=sys.stderr)
            return 2
        result = merge_pull_request(config.project_dir, args.pr, expected_head, dry_run=config.dry_run)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 4

    if args.command == "verify-final-state":
        state = TaskState.load(Path(config.state_dir, "task-state.json"))
        evidence = collect_final_evidence(config.project_dir, state, args.pr)
        report = evaluate_final_state(evidence)
        report_path = config.report_path or str(Path(config.state_dir, "final-report.json"))
        write_final_report(report_path, state, evidence, report)
        print(json.dumps({"ok": report.ok, "checks": report.checks, "failures": report.failures, "evidence": evidence, "report_path": report_path}, indent=2))
        return 0 if report.ok else 4

    monitor = TerminalMonitor(config)

    if config.once:
        inspected = monitor.inspect()
        print(json.dumps(json_safe(inspected), indent=2, sort_keys=True))
        return 0 if inspected.get("ok") else 2

    return monitor.run()

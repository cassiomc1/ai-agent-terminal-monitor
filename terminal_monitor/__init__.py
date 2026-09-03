"""AI Agent Terminal Monitor — universal watcher and safe continuation driver for AI coding agent CLIs.

Zero-dependency package layout (Python 3.10+, macOS Terminal.app / iTerm2 / tmux).
The public SDK surface is defined by ``__all__``; other top-level names are
internal and may change without notice.
"""

from __future__ import annotations

# Kept as package attributes so callers/tests can patch ``terminal_monitor.os``,
# ``terminal_monitor.subprocess`` and ``terminal_monitor.time`` like the
# historical single-module surface.
import os  # noqa: F401
import subprocess  # noqa: F401
import sys  # noqa: F401
import time  # noqa: F401

__version__ = "1.1.0"

from . import backends as backends
from . import classify as classify
from . import cli as cli
from . import config as config
from . import github as github
from . import gitinfo as gitinfo
from . import managed_pty as managed_pty
from . import monitor as monitor
from . import processes as processes
from . import profiles as profiles
from . import remote as remote
from . import replay as replay
from . import safety as safety
from . import session_host as session_host
from . import session_protocol as session_protocol
from . import shell_online as shell_online
from . import state as state
from . import status as status
from . import types as types
from . import web as web
from .backends import (  # noqa: F401
    COMMAND_TIMEOUT_SECONDS,
    OSASCRIPT_TIMEOUT_SECONDS,
    BaseTerminalBackend,
    ITerm2Backend,
    TerminalAppBackend,
    TerminalIdentity,
    TmuxBackend,
    applescript_escape,
    applescript_terminal_title_condition,
    get_backend,
    parse_tab_output,
    process_pids,
    run_command,
    run_osascript,
    run_osascript_timeout_message,
    send_to_terminal,
    terminal_tab,
    validate_process_name,
    validate_title_filter,
    validate_web_port,
)
from .classify import (  # noqa: F401
    TODO_ALL_COMPLETE_PATTERN,
    TODO_COMPLETION_RATIO_PATTERN,
    TODO_FINAL_COMPLETE_PATTERN,
    TODO_ITEM_PATTERN,
    TODO_NO_PENDING_PATTERN,
    _explicit_todo_completion,
    classify_state,
    decide_question,
    extract_options,
    extract_todo_progress,
    find_full_task_titles,
    infer_current_task_id,
    reconcile_task_labels,
    redact_snapshot,
)
from .cli import _add_monitor_args, build_parser, config_from_args, main  # noqa: F401
from .config import (  # noqa: F401
    CONFIG_FILENAMES,
    MonitorConfig,
    discover_config_file,
    generate_starter_config,
    load_config_file,
    tomllib,
)
from .github import (  # noqa: F401
    CODE_FAILURE_CONCLUSIONS,
    EXTERNAL_FAILURE_MARKERS,
    PASSED_CHECK_CONCLUSIONS,
    RETRYABLE_CHECK_CONCLUSIONS,
    FinalVerificationReport,
    PullRequestStateMachine,
    _command_value,
    _parent_pid,
    build_restart_command,
    capture_safety_baseline,
    classify_check_result,
    collect_final_evidence,
    evaluate_final_state,
    evaluate_repository_safety,
    get_current_pr_snapshot,
    git_activity_fingerprint,
    merge_pull_request,
    persist_restart_event,
    retry_infrastructure_checks,
    verify_merge_gate,
    wait_for_change,
    wait_for_ci_event,
    write_final_report,
)
from .gitinfo import (  # noqa: F401
    _GIT_STATUS_CACHE,
    GIT_STATUS_TTL_SECONDS,
    GitStatus,
    _get_git_status_uncached,
    discover_agent_project_dir,
    dispatch_webhook,
    extract_test_progress,
    generate_smart_nudge,
    get_git_status,
    resolve_project_state_dir,
    send_desktop_notification,
)
from .managed_pty import (  # noqa: F401
    ManagedPTYBackend,
    ManagedSessionClient,
    ManagedSessionStatus,
    managed_session_is_reconnectable,
)
from .monitor import TerminalMonitor
from .processes import (  # noqa: F401
    EXPENSIVE_COMMAND_PATTERNS,
    GIT_HISTORY_REWRITE_PATTERNS,
    AgentLoopGuard,
    LoopAssessment,
    ProcessActivity,
    _children_pids,
    _elapsed_seconds,
    assess_agent_commands,
    canonical_expensive_command,
    collect_process_activity,
    interrupt_child,
    interrupt_process_tree,
    pid_is_alive,
    process_is_running,
)
from .profiles import (  # noqa: F401
    BUILTIN_PROFILES,
    AgentProfile,
    clean_option,
    get_profile,
    list_profiles,
)
from .remote import RemoteProvider, RemoteShare  # noqa: F401
from .replay import ReplayBuffer
from .safety import (  # noqa: F401
    DEFAULT_PREFERRED_ANSWERS,
    HIGH_RISK_ACTION_MARKERS,
    NPM_NEGATION_PATTERN,
    NPM_PUBLICATION_PATTERNS,
    SENSITIVE_VALUE_PATTERNS,
    SPECIAL_KEY_CODES,
    UNSAFE_PHRASES,
    PolicyEnvelope,
    _contains_positive_npm_publication,
    classify_action_risk,
    is_unsafe,
    redact_sensitive,
)
from .session_host import SessionHost, SessionHostConfig
from .session_protocol import (  # noqa: F401
    MAX_CONTROL_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    SessionProtocolError,
    encode_message,
    receive_message,
    send_message,
)
from .shell_online import ShellOnlineLaunchResult, ShellOnlineProvider  # noqa: F401
from .state import (  # noqa: F401
    ATTEMPT_STATUSES,
    AttemptLedger,
    SessionTracker,
    StateFileError,
    TaskState,
    _atomic_json_write,
    _atomic_text_write,
    append_log,
    consume_manual_answer,
    is_table_or_box_line,
    json_safe,
    match_pattern,
    normalize_snapshot,
    now_iso,
)
from .status import (  # noqa: F401
    ANSI_CODES,
    _ansi,
    _monitor_process_matches,
    _public_activity,
    _public_attempt,
    _public_event_line,
    _public_status,
    _redacted_commands,
    _status_monitor_label,
    read_status_snapshot,
    render_status_dashboard,
    resume_monitor,
    stop_monitor,
)
from .web import (  # noqa: F401
    DASHBOARD_HTML,
    WEB_POST_BODY_LIMIT_BYTES,
    MonitorWebServer,
)

__all__ = [
    "BUILTIN_PROFILES",
    "UNSAFE_PHRASES",
    "AgentLoopGuard",
    "AgentProfile",
    "AttemptLedger",
    "BaseTerminalBackend",
    "FinalVerificationReport",
    "GitStatus",
    "ITerm2Backend",
    "LoopAssessment",
    "ManagedPTYBackend",
    "ManagedSessionClient",
    "ManagedSessionStatus",
    "MonitorConfig",
    "MonitorWebServer",
    "PolicyEnvelope",
    "ProcessActivity",
    "PullRequestStateMachine",
    "RemoteShare",
    "ReplayBuffer",
    "SessionHost",
    "SessionHostConfig",
    "SessionProtocolError",
    "SessionTracker",
    "ShellOnlineProvider",
    "StateFileError",
    "TaskState",
    "TerminalAppBackend",
    "TerminalIdentity",
    "TerminalMonitor",
    "TmuxBackend",
    "__version__",
    "build_parser",
    "classify_action_risk",
    "classify_state",
    "config_from_args",
    "consume_manual_answer",
    "decide_question",
    "extract_todo_progress",
    "generate_starter_config",
    "get_backend",
    "get_git_status",
    "get_profile",
    "interrupt_child",
    "interrupt_process_tree",
    "is_table_or_box_line",
    "json_safe",
    "list_profiles",
    "load_config_file",
    "main",
    "read_status_snapshot",
    "redact_sensitive",
    "render_status_dashboard",
    "resume_monitor",
    "run_command",
    "run_osascript",
    "stop_monitor",
    "terminal_tab",
    "validate_process_name",
    "validate_title_filter",
    "validate_web_port",
]

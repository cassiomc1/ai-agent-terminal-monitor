import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

MODULE_PATH = pathlib.Path(__file__).parents[1] / "terminal_monitor.py"
SPEC = importlib.util.spec_from_file_location("terminal_monitor", MODULE_PATH)
terminal_monitor = importlib.util.module_from_spec(SPEC)
sys.modules["terminal_monitor"] = terminal_monitor
SPEC.loader.exec_module(terminal_monitor)


class MockBackend(terminal_monitor.BaseTerminalBackend):
    def __init__(self, tab_response: dict | None = None, send_response: tuple[bool, str] = (True, "SENT")):
        self.tab_response = tab_response or {"ok": True, "error": "", "hist": "Ready for input\n"}
        self.send_response = send_response
        self.sent_payloads: list[str] = []
        self.sent_keys: list[str] = []
        self.pids = [12345]

    def name(self) -> str:
        return "mock"

    def get_tab(self, process: str, title: str | None = None) -> dict[str, str | bool]:
        return self.tab_response

    def send(self, process: str, title: str | None, payload: str) -> tuple[bool, str]:
        self.sent_payloads.append(payload)
        return self.send_response

    def send_key(self, process: str, title: str | None, key: str) -> tuple[bool, str]:
        self.sent_keys.append(key)
        return self.send_response

    def get_pids(self, process: str) -> list[int]:
        return self.pids


class MonitorBehaviorTests(unittest.TestCase):
    def test_classifies_permission_over_thinking_markers(self):
        history = "Preparing write... esc to cancel\nAllow once / Deny"
        self.assertEqual(terminal_monitor.classify_state(history), "permission")

    def test_classifies_permission_prompt(self):
        self.assertEqual(
            terminal_monitor.classify_state("Permission required\nAllow once\nDeny"),
            "permission",
        )

    def test_classifies_numbered_question(self):
        history = "Which option should I use?\n1. Continue safely\n2. Stop"
        self.assertEqual(terminal_monitor.classify_state(history), "question")

    def test_recommended_safe_option_is_selected(self):
        history = "Which option?\n○ Continue with validation (Recommended)\n○ Disable validator"
        self.assertEqual(
            terminal_monitor.decide_question(history), "Continue with validation"
        )

    def test_unsafe_options_are_never_selected(self):
        history = "Which option?\n1. Disable validator (Recommended)\n2. Delete ledger"
        self.assertIsNone(terminal_monitor.decide_question(history))

    def test_table_output_not_classified_as_question(self):
        table_history = """
│ Task │ Status │ Description │
├──────┼────────┼─────────────┤
│ 1    │ Done   │ Setup repo  │
│ 2    │ Done   │ Write tests │
└──────┴────────┴─────────────┘
Working directory clean.
"""
        self.assertTrue(terminal_monitor.is_table_or_box_line("│ 1 │ Done │"))
        self.assertTrue(terminal_monitor.is_table_or_box_line("├──────┼────────┤"))
        self.assertEqual(terminal_monitor.classify_state(table_history), "idle")

    def test_completion_detection(self):
        hist = "Test Files 45 passed (45)\nO plano está 100% concluído — não há próxima task a implementar.\n"
        self.assertEqual(terminal_monitor.classify_state(hist), "completed")

    def test_tab_parser_keeps_history_with_equals_signs(self):
        raw = (
            "WIN=1\nTAB=2\nTITLE=worker\nBUSY=false\nWNAME=Terminal\n"
            "HIST=result=a=b\nnext line"
        )
        parsed = terminal_monitor.parse_tab_output(raw)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["tab"], "2")
        self.assertEqual(parsed["hist"], "result=a=b\nnext line")

    def test_process_name_rejects_applescript_injection(self):
        with self.assertRaises(ValueError):
            terminal_monitor.validate_process_name('opencode" then return "owned')

    @unittest.skipUnless(pathlib.Path("/usr/bin/osascript").exists(), "requires macOS")
    def test_missing_process_returns_clean_missing_result(self):
        result = terminal_monitor.terminal_tab("codex_monitor_no_such_process_987")
        self.assertFalse(result["ok"])
        # Headless CI runners may hang osascript until the hard timeout kicks
        # in; both outcomes mean "no tab found" from the caller's perspective.
        acceptable = {
            "matching Terminal tab not found",
            terminal_monitor.run_osascript_timeout_message(),
        }
        self.assertIn(result["error"], acceptable)

    def test_build_parser_requires_text_unless_file_is_given(self):
        parser = terminal_monitor.build_parser()
        args = parser.parse_args(["--continue-text", "Continue now", "--once"])
        config = terminal_monitor.config_from_args(args)
        self.assertEqual(config.continue_text, "Continue now")
        self.assertTrue(config.once)

    def test_manual_answer_is_consumed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            answer_file = pathlib.Path(directory) / "answer.txt"
            answer_file.write_text("Use option B\n", encoding="utf-8")
            self.assertEqual(
                terminal_monitor.consume_manual_answer(str(answer_file)), "Use option B"
            )
            self.assertFalse(answer_file.exists())
            self.assertIsNone(terminal_monitor.consume_manual_answer(str(answer_file)))


class AgentProfilesTests(unittest.TestCase):
    def test_builtin_profiles_exist(self):
        profiles = terminal_monitor.list_profiles()
        for name in ("claude", "opencode", "aider", "goose", "generic"):
            self.assertIn(name, profiles)

    def test_opencode_mode_and_plan_detection(self):
        opencode_prof = terminal_monitor.get_profile("opencode")
        self.assertEqual(opencode_prof.detect_mode("Plan · Ox Alpha\nReading file..."), "plan")
        self.assertEqual(opencode_prof.detect_mode("Build · Ox Alpha\nWriting command..."), "build")
        self.assertTrue(opencode_prof.is_plan_ready("Plano pronto. Aprove para eu sair do modo plano."))
        self.assertTrue(opencode_prof.matches_completion("Todas as tarefas estão concluídas."))

    def test_claude_profile_detection(self):
        claude_prof = terminal_monitor.get_profile("claude")
        self.assertEqual(claude_prof.process, "claude")
        self.assertEqual(claude_prof.auto_permission_payload, "y")

        hist = "Reading files...\nClaude is thinking... esc to cancel"
        self.assertEqual(terminal_monitor.classify_state(hist, claude_prof), "thinking")

        hist_perm = "Allow this tool? [y/n]\nDo you want to run bash command?"
        self.assertEqual(terminal_monitor.classify_state(hist_perm, claude_prof), "permission")

    def test_custom_profile_registration(self):
        custom = {
            "custom-bot": {
                "process": "custombot",
                "thinking_patterns": ["bot is busy", "calculating"],
                "permission_patterns": ["confirm action?"],
                "auto_permission_payload": "yes",
                "mode_patterns": {"plan": "PlanMode", "act": "ActMode"},
                "completion_patterns": ["done everything"],
            }
        }
        prof = terminal_monitor.get_profile("custom-bot", custom_profiles=custom)
        self.assertEqual(prof.name, "custom-bot")
        self.assertEqual(prof.process, "custombot")
        self.assertEqual(prof.auto_permission_payload, "yes")
        self.assertEqual(terminal_monitor.classify_state("bot is busy", prof), "thinking")
        self.assertEqual(terminal_monitor.classify_state("confirm action?", prof), "permission")
        self.assertEqual(prof.detect_mode("Entering PlanMode now"), "plan")
        self.assertEqual(terminal_monitor.classify_state("done everything", prof), "completed")


class ConfigLoadingTests(unittest.TestCase):
    def test_load_json_config(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg_path = pathlib.Path(directory) / ".terminal-monitor.json"
            cfg_path.write_text(
                json.dumps({
                    "profile": "claude",
                    "process": "claude",
                    "continue_text": "Prossiga a partir daqui.",
                    "poll_seconds": 2.5,
                    "max_sends": 50,
                    "auto_allow_permissions": True,
                    "supervise": True,
                    "smart_nudges": True,
                }),
                encoding="utf-8",
            )

            discovered = terminal_monitor.discover_config_file(directory)
            self.assertEqual(discovered, cfg_path.resolve())

            loaded = terminal_monitor.load_config_file(cfg_path)
            self.assertEqual(loaded["profile"], "claude")
            self.assertEqual(loaded["poll_seconds"], 2.5)

            parser = terminal_monitor.build_parser()
            args = parser.parse_args(["--project-dir", directory])
            config = terminal_monitor.config_from_args(args)
            self.assertEqual(config.profile, "claude")
            self.assertEqual(config.process, "claude")
            self.assertEqual(config.continue_text, "Prossiga a partir daqui.")
            self.assertEqual(config.poll_seconds, 2.5)
            self.assertEqual(config.max_sends, 50)
            self.assertTrue(config.auto_allow_permissions)
            self.assertTrue(config.supervise)

    @unittest.skipUnless(terminal_monitor.tomllib is not None, "requires TOML support (Python 3.11+ or tomli)")
    def test_load_toml_config_if_available(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg_path = pathlib.Path(directory) / ".terminal-monitor.toml"
            cfg_path.write_text(
                'profile = "aider"\nprocess = "aider"\ncontinue_text = "Continue"\npoll_seconds = 4.0\n',
                encoding="utf-8",
            )
            discovered = terminal_monitor.discover_config_file(directory)
            self.assertEqual(discovered, cfg_path.resolve())

            parser = terminal_monitor.build_parser()
            args = parser.parse_args(["--project-dir", directory])
            config = terminal_monitor.config_from_args(args)
            self.assertEqual(config.profile, "aider")
            self.assertEqual(config.poll_seconds, 4.0)

    def test_generate_starter_config(self):
        json_starter = terminal_monitor.generate_starter_config("json")
        self.assertIn("profile", json_starter)
        parsed = json.loads(json_starter)
        self.assertIn("custom_profiles", parsed)
        self.assertTrue(parsed.get("supervise"))

        toml_starter = terminal_monitor.generate_starter_config("toml")
        self.assertIn("profile =", toml_starter)


class BackendTests(unittest.TestCase):
    def test_get_backend_resolution(self):
        self.assertEqual(terminal_monitor.get_backend("terminal").name(), "terminal")
        self.assertEqual(terminal_monitor.get_backend("iterm2").name(), "iterm2")
        self.assertEqual(terminal_monitor.get_backend("tmux").name(), "tmux")
        with self.assertRaises(ValueError):
            terminal_monitor.get_backend("invalid-backend-name")

    def test_special_key_constants(self):
        self.assertEqual(terminal_monitor.SPECIAL_KEY_CODES["tab"], 9)
        self.assertEqual(terminal_monitor.SPECIAL_KEY_CODES["esc"], 27)
        self.assertEqual(terminal_monitor.SPECIAL_KEY_CODES["ctrl+c"], 3)
        self.assertEqual(terminal_monitor.SPECIAL_KEY_CODES["ctrl+p"], 16)


class GitContextTests(unittest.TestCase):
    def test_smart_nudge_generation(self):
        dirty_status = terminal_monitor.GitStatus(is_repo=True, branch="feat/task-1", dirty=True, modified_count=2)
        nudge_dirty = terminal_monitor.generate_smart_nudge(dirty_status)
        self.assertIn("uncommitted changes", nudge_dirty)
        self.assertIn("feat/task-1", nudge_dirty)

        clean_branch_status = terminal_monitor.GitStatus(is_repo=True, branch="feat/task-1", dirty=False)
        nudge_clean = terminal_monitor.generate_smart_nudge(clean_branch_status)
        self.assertIn("working tree", nudge_clean)
        self.assertIn("Pull Request", nudge_clean)

        open_pr_status = terminal_monitor.GitStatus(is_repo=True, branch="feat/task-1", open_prs_count=1)
        nudge_pr = terminal_monitor.generate_smart_nudge(open_pr_status)
        self.assertIn("Pull Requests", nudge_pr)


class TerminalMonitorClassTests(unittest.TestCase):
    def test_monitor_inspect(self):
        backend = MockBackend(tab_response={"ok": True, "error": "", "hist": "esc interrupt\nworking..."})
        config = terminal_monitor.MonitorConfig(process="opencode", continue_text="Go")
        monitor = terminal_monitor.TerminalMonitor(config, backend=backend)
        inspected = monitor.inspect()
        self.assertTrue(inspected["ok"])
        self.assertEqual(inspected["state"], "thinking")
        self.assertEqual(inspected["pids"], [12345])

    def test_monitor_step_sends_payload_when_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = MockBackend(tab_response={"ok": True, "error": "", "hist": "Ready for next prompt."})
            config = terminal_monitor.MonitorConfig(
                process="claude",
                profile="claude",
                continue_text="Prossiga com os testes",
                idle_seconds=0.0,
                cooldown_seconds=0.0,
                smart_nudges=False,
                state_dir=directory,
            )
            monitor = terminal_monitor.TerminalMonitor(config, backend=backend)

            events = []
            monitor.on_send = lambda reason, payload, ok: events.append((reason, payload, ok))

            code, msg = monitor.step()
            self.assertIsNone(code)
            self.assertIn("SENT", msg)
            self.assertEqual(backend.sent_payloads, ["Prossiga com os testes"])
            self.assertEqual(events, [("idle", "Prossiga com os testes", True)])

    def test_monitor_step_mode_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = MockBackend(tab_response={
                "ok": True,
                "error": "",
                "hist": "Plan · Ox Alpha\nPlano pronto. Aprove para eu sair do modo plano.",
            })
            config = terminal_monitor.MonitorConfig(
                process="opencode",
                profile="opencode",
                continue_text="Prossiga",
                idle_seconds=0.0,
                cooldown_seconds=0.0,
                auto_switch_modes=True,
                state_dir=directory,
            )
            monitor = terminal_monitor.TerminalMonitor(config, backend=backend)

            mode_changes = []
            monitor.on_mode_change = lambda old, new: mode_changes.append((old, new))

            code, msg = monitor.step()
            self.assertIsNone(code)
            self.assertEqual(msg, "MODE_SWITCH_SENT")
            self.assertEqual(backend.sent_keys, ["tab"])
            self.assertEqual(mode_changes, [(None, "plan")])

    def test_monitor_step_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = MockBackend(tab_response={
                "ok": True,
                "error": "",
                "hist": "O plano está 100% concluído — não há próxima task a implementar.",
            })
            config = terminal_monitor.MonitorConfig(
                process="opencode",
                profile="opencode",
                completion_check=True,
                state_dir=directory,
            )
            monitor = terminal_monitor.TerminalMonitor(config, backend=backend)

            completed = []
            monitor.on_complete = lambda snap: completed.append(snap)

            code, msg = monitor.step()
            self.assertEqual(code, 0)
            self.assertEqual(msg, "COMPLETED")
            self.assertEqual(len(completed), 1)

    def test_monitor_step_status_json_export(self):
        with tempfile.TemporaryDirectory() as directory:
            status_file = pathlib.Path(directory) / "status.json"
            backend = MockBackend(tab_response={"ok": True, "error": "", "hist": "working...\nesc interrupt"})
            config = terminal_monitor.MonitorConfig(
                process="opencode",
                profile="opencode",
                state_dir=directory,
                status_json_path=str(status_file),
            )
            monitor = terminal_monitor.TerminalMonitor(config, backend=backend)
            monitor.step()

            self.assertTrue(status_file.exists())
            data = json.loads(status_file.read_text(encoding="utf-8"))
            self.assertEqual(data["process"], "opencode")
            self.assertEqual(data["state"], "thinking")
            self.assertTrue(data["running"])

    def test_monitor_step_stop_file(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = MockBackend()
            config = terminal_monitor.MonitorConfig(
                process="claude",
                continue_text="Go",
                state_dir=directory,
            )
            monitor = terminal_monitor.TerminalMonitor(config, backend=backend)
            stop_file = pathlib.Path(directory) / "stop"
            stop_file.touch()

            code, msg = monitor.step()
            self.assertEqual(code, 0)
            self.assertEqual(msg, "CANCELLED")

    def test_monitor_step_attention_required(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = MockBackend(tab_response={
                "ok": True,
                "error": "",
                "hist": "Question: which dangerous command to run?\n1. Delete database\n2. Skip validation",
            })
            config = terminal_monitor.MonitorConfig(
                process="claude",
                continue_text="Go",
                idle_seconds=0.0,
                cooldown_seconds=0.0,
                smart_nudges=False,
                state_dir=directory,
            )
            monitor = terminal_monitor.TerminalMonitor(config, backend=backend)

            code, msg = monitor.step()
            self.assertEqual(code, 3)
            self.assertIn("ATTENTION_REQUIRED", msg)
            attention_file = pathlib.Path(directory) / "attention.txt"
            self.assertTrue(attention_file.exists())


class TmuxBackendTests(unittest.TestCase):
    def test_find_target_matches_command_and_title(self):
        panes = (
            "session:0.0 vim main window\n"
            "session:0.1 opencode agent session\n"
        )
        backend = terminal_monitor.TmuxBackend()
        with mock.patch("shutil.which", return_value="/usr/bin/tmux"), mock.patch.object(
            terminal_monitor, "run_command", return_value=(0, panes, "")
        ):
            self.assertEqual(backend._find_target("opencode"), "session:0.1")
            self.assertEqual(backend._find_target("vim", title="main"), "session:0.0")
            self.assertIsNone(backend._find_target("vim", title="nope"))
            self.assertIsNone(backend._find_target("missing-process"))

    def test_find_target_requires_tmux_binary(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(terminal_monitor.TmuxBackend()._find_target("opencode"))


class RobustnessTests(unittest.TestCase):
    def test_run_osascript_timeout_returns_error(self):
        def raise_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="osascript", timeout=15)

        with mock.patch.object(terminal_monitor.subprocess, "run", side_effect=raise_timeout):
            code, detail = terminal_monitor.run_osascript('return "x"')
        self.assertEqual(code, 1)
        self.assertIn("timed out", detail)

    def test_run_command_timeout_returns_error(self):
        def raise_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=30)

        with mock.patch.object(terminal_monitor.subprocess, "run", side_effect=raise_timeout):
            code, _out, err = terminal_monitor.run_command(["git", "status"])
        self.assertEqual(code, 1)
        self.assertIn("timed out", err)

    def test_title_filter_validation(self):
        with self.assertRaises(ValueError):
            terminal_monitor.validate_title_filter("bad\ninjection")
        with self.assertRaises(ValueError):
            terminal_monitor.validate_title_filter("x" * 201)
        self.assertIsNone(terminal_monitor.validate_title_filter(None))
        self.assertIsNone(terminal_monitor.validate_title_filter("   "))
        self.assertEqual(terminal_monitor.validate_title_filter(" worker "), "worker")

    def test_git_status_cache_respects_ttl(self):
        calls = []
        fake_status = terminal_monitor.GitStatus(is_repo=True, branch="cached-branch")

        def fake_uncached(repo_dir="."):
            calls.append(repo_dir)
            return fake_status

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            terminal_monitor, "_get_git_status_uncached", side_effect=fake_uncached
        ):
            first = terminal_monitor.get_git_status(directory, ttl_seconds=60.0)
            second = terminal_monitor.get_git_status(directory, ttl_seconds=60.0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(first.branch, second.branch)
            refreshed = terminal_monitor.get_git_status(directory, ttl_seconds=0.0)
            self.assertEqual(len(calls), 2)
            self.assertEqual(refreshed.branch, "cached-branch")


    def test_unsafe_phrases_merge_file_and_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg_path = pathlib.Path(directory) / ".terminal-monitor.json"
            cfg_path.write_text(
                json.dumps({"process": "claude", "unsafe_phrases": ["custom-danger", "rm -rf"]}),
                encoding="utf-8",
            )
            parser = terminal_monitor.build_parser()
            args = parser.parse_args(["--project-dir", directory, "--unsafe-phrase", "cli-only-risk"])
            config = terminal_monitor.config_from_args(args)

            self.assertIn("custom-danger", config.unsafe_phrases)
            self.assertIn("cli-only-risk", config.unsafe_phrases)
            self.assertIn("rm -rf", config.unsafe_phrases)
            self.assertEqual(len(config.unsafe_phrases), len(set(config.unsafe_phrases)))

    def test_supervise_flags(self):
        with tempfile.TemporaryDirectory() as empty_dir:
            parser = terminal_monitor.build_parser()

            args = parser.parse_args(["--supervise", "--project-dir", empty_dir])
            config = terminal_monitor.config_from_args(args)
            self.assertTrue(config.supervise)
            self.assertTrue(config.auto_allow_permissions)
            self.assertTrue(config.smart_nudges)

            args = parser.parse_args([
                "supervise", "--no-smart-nudges", "--no-mode-switch",
                "--no-completion-check", "--project-dir", empty_dir,
            ])
            config = terminal_monitor.config_from_args(args)
            self.assertTrue(config.supervise)
            self.assertFalse(config.smart_nudges)
            self.assertFalse(config.auto_switch_modes)
            self.assertFalse(config.completion_check)


class SupervisorV2Tests(unittest.TestCase):
    def test_attempt_ledger_records_idempotent_lifecycle(self):
        ledger = terminal_monitor.AttemptLedger(max_records=10)
        attempt_id = ledger.queue("idle", "Continue safely", observed_state="idle")
        ledger.transition(attempt_id, "sent", detail="SENT")
        ledger.transition(attempt_id, "accepted", detail="backend accepted")
        ledger.transition(attempt_id, "completed", observed_state="thinking")

        self.assertEqual(attempt_id, ledger.records[0]["attempt_id"])
        self.assertEqual(
            [event["status"] for event in ledger.records],
            ["queued", "sent", "accepted", "completed"],
        )

    def test_ci_check_classification_distinguishes_code_and_external_failures(self):
        self.assertEqual(
            terminal_monitor.classify_check_result({"conclusion": "success"})["category"],
            "passed",
        )
        self.assertEqual(
            terminal_monitor.classify_check_result({"conclusion": "cancelled"})["category"],
            "cancelled-infra",
        )
        external = terminal_monitor.classify_check_result(
            {"conclusion": "failure", "output": "429 Too Many Requests while checking links"}
        )
        self.assertEqual(external["category"], "failed-external")
        self.assertTrue(external["retryable"])
        self.assertEqual(
            terminal_monitor.classify_check_result(
                {"conclusion": "failure", "output": "assertion failed in unit test"}
            )["category"],
            "failed",
        )

    def test_policy_blocks_publish_and_release_actions(self):
        policy = terminal_monitor.PolicyEnvelope(
            objective="Finish the task safely.",
            prohibitions=("Do not publish to npm.",),
        )
        self.assertEqual(terminal_monitor.classify_action_risk("npm publish"), "blocked")
        self.assertEqual(terminal_monitor.classify_action_risk("gh release create v1.0.0"), "attention")
        allowed, reason = policy.authorize_action("npm publish")
        self.assertFalse(allowed)
        self.assertIn("npm", reason.lower())
        safe_policy_text = policy.compose("Run the next tests.")
        allowed, _reason = policy.authorize_action(safe_policy_text)
        self.assertTrue(allowed)
        self.assertEqual(terminal_monitor.classify_action_risk("Do not npm publish."), "safe")

    def test_merge_gate_requires_fresh_exact_head_and_green_checks(self):
        pr = {
            "number": 7,
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "statusCheckRollup": [{"name": "tests", "conclusion": "success"}],
        }
        with mock.patch.object(
            terminal_monitor,
            "run_command",
            return_value=(0, json.dumps(pr), ""),
        ) as run:
            result = terminal_monitor.verify_merge_gate(".", 7, "a" * 40)
        self.assertTrue(result["ok"])
        self.assertIn("--json", run.call_args.args[0])

        pr["headRefOid"] = "b" * 40
        with mock.patch.object(
            terminal_monitor,
            "run_command",
            return_value=(0, json.dumps(pr), ""),
        ):
            result = terminal_monitor.verify_merge_gate(".", 7, "a" * 40)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "head_mismatch")

    def test_merge_uses_exact_head_match_after_gate(self):
        head = "c" * 40
        pr = {
            "number": 8,
            "state": "OPEN",
            "headRefOid": head,
            "statusCheckRollup": [{"name": "tests", "conclusion": "success"}],
        }
        with mock.patch.object(
            terminal_monitor,
            "run_command",
            side_effect=[(0, json.dumps(pr), ""), (0, "merged", "")],
        ) as run:
            result = terminal_monitor.merge_pull_request(".", 8, head)
        self.assertTrue(result["merged"])
        merge_command = run.call_args_list[1].args[0]
        self.assertIn("--match-head-commit", merge_command)
        self.assertIn(head, merge_command)

    def test_merge_gate_treats_already_merged_pr_as_completed(self):
        head = "d" * 40
        pr = {"number": 9, "state": "MERGED", "headRefOid": head, "statusCheckRollup": []}
        with mock.patch.object(terminal_monitor, "run_command", return_value=(0, json.dumps(pr), "")):
            result = terminal_monitor.merge_pull_request(".", 9, head)
        self.assertTrue(result["ok"])
        self.assertTrue(result["merged"])
        self.assertEqual(result["reason"], "already_merged")

    def test_retry_only_targets_infrastructure_like_checks(self):
        pr = {
            "statusCheckRollup": [
                {"name": "cancelled-job", "conclusion": "cancelled", "detailsUrl": "https://github.com/x/actions/runs/111"},
                {"name": "code-job", "conclusion": "failure", "detailsUrl": "https://github.com/x/actions/runs/222"},
            ]
        }
        with mock.patch.object(terminal_monitor, "run_command", return_value=(0, "", "")) as run:
            retried = terminal_monitor.retry_infrastructure_checks(".", pr)
        self.assertEqual(retried, [111])
        self.assertEqual(run.call_args.args[0], ["gh", "run", "rerun", "111", "--failed"])

    def test_dry_run_merge_never_calls_github(self):
        with mock.patch.object(terminal_monitor, "run_command") as run:
            result = terminal_monitor.merge_pull_request(".", 7, "a" * 40, dry_run=True)
        self.assertTrue(result["dry_run"])
        run.assert_not_called()

    def test_merge_gate_rejects_non_full_head_sha(self):
        with mock.patch.object(terminal_monitor, "run_command") as run:
            result = terminal_monitor.verify_merge_gate(".", 7, "abc123")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid_expected_head")
        run.assert_not_called()

    def test_repository_safety_detects_dirty_protected_branch(self):
        status = terminal_monitor.GitStatus(
            is_repo=True,
            branch="main",
            head="a" * 40,
            dirty=True,
            modified_count=1,
            modified_files=("terminal_monitor.py",),
        )
        result = terminal_monitor.evaluate_repository_safety(status, expected_branch="codex/work")
        self.assertFalse(result["safe"])
        self.assertEqual(result["reason"], "protected_branch_dirty")
        self.assertEqual(result["head"], "a" * 40)
        self.assertEqual(result["modified_files"], ["terminal_monitor.py"])

    def test_restart_event_and_final_report_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = pathlib.Path(directory) / "task-state.json"
            report_path = pathlib.Path(directory) / "final-report.json"
            state = terminal_monitor.TaskState(
                session_id="ses-1",
                attempts=({"attempt_id": "a1", "status": "accepted"},),
                prohibitions=("Do not publish to npm.",),
            )
            state = terminal_monitor.persist_restart_event(state_path, state, ["opencode", "--session", "ses-1"])
            self.assertEqual(state.pr["lastRestart"]["sessionId"], "ses-1")
            state.save(state_path)
            terminal_monitor.write_final_report(
                report_path,
                state,
                {"pr_merged": True, "npm_registry_unchanged": True},
                terminal_monitor.FinalVerificationReport(True, {"pr_merged": True}, ()),
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["attempts"][0]["attempt_id"], "a1")
            self.assertFalse(report["npm_publish_allowed"])
            self.assertTrue(report["npm_publication_prohibited"])
            self.assertIn("Do not publish to npm.", report["prohibitions"])

    def test_dry_run_mode_switch_does_not_send_key(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = MockBackend(tab_response={
                "ok": True,
                "error": "",
                "hist": "Plan · Ox Alpha\nPlano pronto. Aprove para eu sair do modo plano.",
            })
            monitor = terminal_monitor.TerminalMonitor(
                terminal_monitor.MonitorConfig(
                    process="opencode",
                    profile="opencode",
                    continue_text="Prossiga",
                    idle_seconds=0.0,
                    cooldown_seconds=0.0,
                    dry_run=True,
                    state_dir=directory,
                ),
                backend=backend,
            )
            code, message = monitor.step()
        self.assertEqual(code, 0)
        self.assertIn("DRY_RUN", message)
        self.assertEqual(backend.sent_keys, [])

    def test_dry_run_permission_does_not_send_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = MockBackend(tab_response={
                "ok": True,
                "error": "",
                "hist": "Permission required\nAllow once\nDeny",
            })
            monitor = terminal_monitor.TerminalMonitor(
                terminal_monitor.MonitorConfig(
                    process="opencode",
                    profile="opencode",
                    auto_allow_permissions=True,
                    idle_seconds=0.0,
                    cooldown_seconds=0.0,
                    dry_run=True,
                    state_dir=directory,
                ),
                backend=backend,
            )
            code, message = monitor.step()
        self.assertEqual(code, 0)
        self.assertIn("DRY_RUN", message)
        self.assertEqual(backend.sent_payloads, [])

    def test_merge_pr_command_is_available(self):
        parser = terminal_monitor.build_parser()
        args = parser.parse_args(["merge-pr", "--pr", "7", "--head", "a" * 40, "--dry-run"])
        self.assertEqual(args.command, "merge-pr")
        self.assertEqual(args.pr, 7)
        self.assertTrue(args.dry_run)

    def test_monitor_step_persists_attempts_in_task_and_status_state(self):
        with tempfile.TemporaryDirectory() as directory:
            status_path = pathlib.Path(directory) / "status.json"
            backend = MockBackend(tab_response={"ok": True, "error": "", "hist": "Ready for next task."})
            monitor = terminal_monitor.TerminalMonitor(
                terminal_monitor.MonitorConfig(
                    process="opencode",
                    profile="opencode",
                    continue_text="Continue safely",
                    idle_seconds=0.0,
                    cooldown_seconds=0.0,
                    smart_nudges=False,
                    state_dir=directory,
                    status_json_path=str(status_path),
                ),
                backend=backend,
            )
            code, message = monitor.step()
            self.assertIsNone(code)
            self.assertIn("SENT", message)
            statuses = [item["status"] for item in monitor.task_state.attempts]
            self.assertEqual(statuses, ["queued", "sent", "accepted"])
            exported = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(exported["attempts"][-1]["status"], "accepted")

    def test_monitor_marks_accepted_attempt_completed_after_new_output(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = MockBackend(tab_response={"ok": True, "error": "", "hist": "Ready for next task."})
            monitor = terminal_monitor.TerminalMonitor(
                terminal_monitor.MonitorConfig(
                    process="opencode",
                    profile="opencode",
                    continue_text="Continue safely",
                    idle_seconds=0.0,
                    cooldown_seconds=0.0,
                    smart_nudges=False,
                    state_dir=directory,
                ),
                backend=backend,
            )
            monitor.step()
            backend.tab_response["hist"] = "Ready for next task.\nAgent started the next task."
            monitor.step()
            statuses = [item["status"] for item in monitor.task_state.attempts]
        self.assertIn("completed", statuses)

    def test_manual_npm_publication_answer_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            pathlib.Path(directory, "answer.txt").write_text("npm publish\n", encoding="utf-8")
            backend = MockBackend(tab_response={"ok": True, "error": "", "hist": "Ready for input."})
            monitor = terminal_monitor.TerminalMonitor(
                terminal_monitor.MonitorConfig(
                    process="opencode",
                    profile="opencode",
                    state_dir=directory,
                    cooldown_seconds=0.0,
                ),
                backend=backend,
            )
            code, message = monitor.step()
        self.assertEqual(code, 3)
        self.assertIn("policy_conflict", message)
        self.assertEqual(backend.sent_payloads, [])

    def test_supervised_monitor_pauses_on_dirty_protected_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = MockBackend()
            dirty_main = terminal_monitor.GitStatus(is_repo=True, branch="main", dirty=True, modified_count=1)
            config = terminal_monitor.MonitorConfig(
                process="opencode",
                profile="opencode",
                supervise=True,
                expected_branch="codex/work",
                state_dir=directory,
            )
            with mock.patch.object(terminal_monitor, "get_git_status", return_value=dirty_main), mock.patch.object(
                terminal_monitor, "capture_safety_baseline", return_value={"safetyBaselineCaptured": True}
            ), mock.patch.object(terminal_monitor, "get_current_pr_snapshot", return_value=None), mock.patch.object(
                terminal_monitor, "collect_process_activity", return_value=terminal_monitor.ProcessActivity()
            ):
                monitor = terminal_monitor.TerminalMonitor(config, backend=backend)
                code, message = monitor.step()
            self.assertEqual(code, 3)
            self.assertIn("repository_safety", message)

    def test_supervision_baselines_current_branch_and_pauses_after_branch_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = MockBackend()
            feature = terminal_monitor.GitStatus(is_repo=True, branch="codex/work")
            switched = terminal_monitor.GitStatus(is_repo=True, branch="main")
            config = terminal_monitor.MonitorConfig(
                process="opencode",
                profile="opencode",
                supervise=True,
                state_dir=directory,
            )
            with mock.patch.object(terminal_monitor, "get_git_status", side_effect=[feature, switched, switched]), mock.patch.object(
                terminal_monitor, "capture_safety_baseline", return_value={"safetyBaselineCaptured": True}
            ), mock.patch.object(terminal_monitor, "get_current_pr_snapshot", return_value=None), mock.patch.object(
                terminal_monitor, "collect_process_activity", return_value=terminal_monitor.ProcessActivity()
            ):
                monitor = terminal_monitor.TerminalMonitor(config, backend=backend)
                code, message = monitor.step()
        self.assertEqual(code, 3)
        self.assertIn("repository_safety", message)

    def test_session_tracker_rejects_completion_from_before_latest_interaction(self):
        tracker = terminal_monitor.SessionTracker()
        old = "All tasks complete.\nReady for input."
        tracker.mark_interaction(old)
        self.assertEqual(tracker.current_segment(old), "")
        self.assertFalse(tracker.matches_current_completion(old, ["all tasks complete"]))
        current = old + "\nWorking on the new task.\nAll tasks complete."
        self.assertTrue(tracker.matches_current_completion(current, ["all tasks complete"]))

    def test_active_child_command_suppresses_question_classification(self):
        history = "Which option should I use?\n1. Continue safely\n2. Stop"
        activity = terminal_monitor.ProcessActivity(active=True, descendants=(4321,), commands=("pytest",))
        self.assertEqual(terminal_monitor.classify_state(history, activity=activity), "thinking")

    def test_persistent_idle_helper_is_not_mistaken_for_active_command(self):
        ps_output = "200 100 02:00:00 0.0 typescript-language-server --stdio"
        with mock.patch.object(terminal_monitor, "run_command", return_value=(0, ps_output, "")):
            activity = terminal_monitor.collect_process_activity([100])
        self.assertFalse(activity.active)
        self.assertEqual(activity.descendants, (200,))

    def test_long_running_test_command_counts_as_activity_even_when_cpu_is_quiet(self):
        ps_output = "200 100 15:00 0.0 python3 -m unittest discover -s tests"
        with mock.patch.object(terminal_monitor, "run_command", return_value=(0, ps_output, "")):
            activity = terminal_monitor.collect_process_activity([100])
        self.assertTrue(activity.active)

    def test_weak_question_text_without_options_is_idle(self):
        self.assertEqual(terminal_monitor.classify_state("Question coverage improved in tests."), "idle")

    def test_manual_answer_has_priority_while_agent_looks_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            pathlib.Path(directory, "answer.txt").write_text("Use option B\n", encoding="utf-8")
            backend = MockBackend(tab_response={"ok": True, "error": "", "hist": "working...\nesc interrupt"})
            monitor = terminal_monitor.TerminalMonitor(
                terminal_monitor.MonitorConfig(
                    process="opencode",
                    state_dir=directory,
                    cooldown_seconds=0,
                ),
                backend=backend,
            )
            _code, message = monitor.step()
            self.assertEqual(backend.sent_payloads, ["Use option B"])
            self.assertIn("kind=manual", message)

    def test_supervisor_does_not_finish_before_required_pr_merge_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            backend = MockBackend(tab_response={"ok": True, "error": "", "hist": "Todas as tarefas estão concluídas."})
            config = terminal_monitor.MonitorConfig(
                process="opencode",
                profile="opencode",
                supervise=True,
                state_dir=directory,
                idle_seconds=999,
            )
            clean_git = terminal_monitor.GitStatus(is_repo=True, branch="codex/work")
            with mock.patch.object(terminal_monitor, "capture_safety_baseline", return_value={"safetyBaselineCaptured": True}), mock.patch.object(
                terminal_monitor, "get_current_pr_snapshot", return_value=None
            ), mock.patch.object(terminal_monitor, "get_git_status", return_value=clean_git), mock.patch.object(
                terminal_monitor, "collect_process_activity", return_value=terminal_monitor.ProcessActivity()
            ):
                monitor = terminal_monitor.TerminalMonitor(config, backend=backend)
                code, message = monitor.step()
            self.assertIsNone(code)
            self.assertIn("WAITING", message)

    def test_policy_envelope_keeps_permanent_prohibitions_in_every_nudge(self):
        policy = terminal_monitor.PolicyEnvelope(
            objective="Finish every task and merge the PR.",
            prohibitions=("Do not publish to npm.",),
        )
        message = policy.compose("Run the next tests.", stage="IMPLEMENTING")
        self.assertIn("Finish every task", message)
        self.assertIn("Do not publish to npm", message)
        self.assertIn("Run the next tests", message)
        with self.assertRaises(ValueError):
            policy.compose("Publish the package to npm now.")

    def test_task_state_round_trip_and_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory, "task-state.json")
            state = terminal_monitor.TaskState(
                objective="Finish tasks",
                prohibitions=("Do not publish npm",),
                plan=("test", "merge"),
                branch="codex/work",
                task_id="task-7",
                required_outcome="merged",
                last_known_stage="CI_PENDING",
                pr={"number": 42, "head": "abc"},
                interaction_marker="latest prompt",
            )
            state.save(path)
            loaded = terminal_monitor.TaskState.load(path)
            self.assertEqual(loaded, state)
            self.assertFalse(loaded.npm_publish_allowed)
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(terminal_monitor.StateFileError):
                terminal_monitor.TaskState.load(path)

    def test_saved_interaction_marker_rejects_stale_completion_after_restart(self):
        state = terminal_monitor.TaskState(interaction_marker="All tasks complete.\nReady for input.", session_generation=2)
        tracker = terminal_monitor.SessionTracker(state.interaction_marker, state.session_generation)
        self.assertFalse(tracker.matches_current_completion(state.interaction_marker, ["all tasks complete"]))

    def test_state_dir_enables_status_json_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = terminal_monitor.TerminalMonitor(
                terminal_monitor.MonitorConfig(process="opencode", state_dir=directory),
                backend=MockBackend(),
            )
            self.assertEqual(monitor.status_json_path, str(pathlib.Path(directory, "status.json")))

    def test_terminal_identity_scores_exact_project_session_branch_and_pid(self):
        identity = terminal_monitor.TerminalIdentity(
            project_path="/work/repo",
            branch="codex/work",
            session_id="ses-9",
            title="OpenCode",
            root_pid=123,
        )
        exact = {"history": "cd /work/repo ses-9 codex/work", "title": "OpenCode", "root_pid": 123}
        other = {"history": "cd /work/other", "title": "OpenCode", "root_pid": 999}
        self.assertGreater(identity.score(exact), identity.score(other))

    def test_interrupt_child_never_signals_root_and_only_signals_descendant(self):
        signalled = []
        parents = {200: 100, 300: 999}
        self.assertFalse(terminal_monitor.interrupt_child({100}, 100, parent_of=parents.get, signaler=lambda pid, sig: signalled.append(pid)))
        self.assertFalse(terminal_monitor.interrupt_child({100}, 300, parent_of=parents.get, signaler=lambda pid, sig: signalled.append(pid)))
        self.assertTrue(terminal_monitor.interrupt_child({100}, 200, parent_of=parents.get, signaler=lambda pid, sig: signalled.append(pid)))
        self.assertEqual(signalled, [200])

    def test_pr_state_machine_distinguishes_code_and_retryable_failures(self):
        machine = terminal_monitor.PullRequestStateMachine()
        self.assertEqual(machine.advance(None), "TASK_RECEIVED")
        self.assertEqual(machine.advance({"number": 7, "state": "OPEN", "checks": []}), "PR_CREATED")
        self.assertEqual(machine.advance({"number": 7, "state": "OPEN", "checks": []}), "CI_PENDING")
        self.assertEqual(machine.advance({"number": 7, "state": "OPEN", "checks": [{"conclusion": "failure"}]}), "FIX_REQUIRED")
        self.assertEqual(machine.advance({"number": 7, "state": "OPEN", "checks": [{"conclusion": "cancelled"}]}), "CI_RETRY_REQUIRED")
        self.assertEqual(machine.advance({"number": 7, "state": "OPEN", "checks": [{"conclusion": "success"}]}), "CI_GREEN")
        self.assertEqual(machine.advance({"number": 7, "state": "MERGED", "checks": [{"conclusion": "success"}]}), "POST_MERGE_VERIFY")

    def test_final_verifier_requires_every_safety_invariant(self):
        good = {
            "pr_merged": True,
            "pr_head": "abc",
            "checked_head": "abc",
            "checks_green": True,
            "local_head": "def",
            "main_head": "def",
            "origin_main_head": "def",
            "worktree_clean": True,
            "npm_registry_unchanged": True,
            "no_new_tag_or_release": True,
            "no_publish_process": True,
        }
        report = terminal_monitor.evaluate_final_state(good)
        self.assertTrue(report.ok)
        broken = dict(good, checked_head="stale")
        report = terminal_monitor.evaluate_final_state(broken)
        self.assertFalse(report.ok)
        self.assertIn("checks_exact_head", report.failures)

    def test_final_evidence_fails_closed_without_release_baseline(self):
        state = terminal_monitor.TaskState(pr={})
        commands = {
            ("git", "rev-parse", "HEAD"): (0, "def", ""),
            ("git", "rev-parse", "main"): (0, "def", ""),
            ("git", "rev-parse", "origin/main"): (0, "def", ""),
            ("git", "status", "--porcelain"): (0, "", ""),
            ("git", "tag", "--list"): (0, "", ""),
            ("pgrep", "-af", "(?:^|/)(?:npm|pnpm|yarn)(?:\\s|$)"): (1, "", ""),
        }
        with mock.patch.object(terminal_monitor, "run_command", side_effect=lambda cmd, cwd=None: commands.get(tuple(cmd), (1, "", ""))), mock.patch(
            "shutil.which", return_value=None
        ):
            evidence = terminal_monitor.collect_final_evidence(".", state)
        self.assertFalse(evidence["no_new_tag_or_release"])

    def test_final_evidence_ignores_monitor_args_that_mention_npm_publish(self):
        state = terminal_monitor.TaskState(pr={"safetyBaselineCaptured": True, "tagsBefore": [], "releasesBefore": []})
        commands = {
            ("git", "rev-parse", "HEAD"): (0, "def", ""),
            ("git", "rev-parse", "main"): (0, "def", ""),
            ("git", "rev-parse", "origin/main"): (0, "def", ""),
            ("git", "status", "--porcelain"): (0, "", ""),
            ("git", "tag", "--list"): (0, "", ""),
            ("pgrep", "-af", "(?:^|/)(?:npm|pnpm|yarn)(?:\\s|$)"): (
                0,
                "70135 python terminal_monitor.py --prohibition Do not publish to npm",
                "",
            ),
        }
        with mock.patch.object(
            terminal_monitor,
            "run_command",
            side_effect=lambda cmd, cwd=None: commands.get(tuple(cmd), (1, "", "")),
        ), mock.patch("shutil.which", return_value=None):
            evidence = terminal_monitor.collect_final_evidence(".", state)
        self.assertTrue(evidence["no_publish_process"])

    def test_change_waiter_returns_when_fingerprint_changes(self):
        values = iter(["same", "same", "changed"])
        sleeps = []
        changed = terminal_monitor.wait_for_change(
            lambda: next(values),
            "same",
            timeout_seconds=10,
            interval_seconds=0.1,
            sleeper=lambda seconds: sleeps.append(seconds),
        )
        self.assertTrue(changed)
        self.assertEqual(len(sleeps), 2)

    def test_ci_wait_uses_github_watch_instead_of_sleep_polling(self):
        completed = subprocess.CompletedProcess([], 0, stdout="checks passed", stderr="")
        with mock.patch.object(terminal_monitor.subprocess, "run", return_value=completed) as run:
            self.assertTrue(terminal_monitor.wait_for_ci_event(".", 42, timeout_seconds=8))
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["gh", "pr", "checks", "42"])
        self.assertIn("--watch", command)

    def test_new_operational_commands_parse(self):
        parser = terminal_monitor.build_parser()
        self.assertEqual(parser.parse_args(["send", "resume work"]).command, "send")
        self.assertEqual(parser.parse_args(["interrupt-child", "--pid", "321"]).pid, 321)
        restart = parser.parse_args(["restart-agent", "--continue-session"])
        self.assertTrue(restart.continue_session)
        self.assertEqual(parser.parse_args(["verify-final-state"]).command, "verify-final-state")


if __name__ == "__main__":
    unittest.main()

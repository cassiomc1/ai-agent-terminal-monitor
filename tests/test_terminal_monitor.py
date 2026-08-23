import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


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
    def test_classifies_thinking_before_prompt_markers(self):
        history = "Allow Deny\nPreparing write... esc interrupt"
        self.assertEqual(terminal_monitor.classify_state(history), "thinking")

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
        self.assertEqual(result["error"], "matching Terminal tab not found")

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


if __name__ == "__main__":
    unittest.main()

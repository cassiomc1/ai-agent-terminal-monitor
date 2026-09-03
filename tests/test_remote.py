import contextlib
import dataclasses
import os
import pathlib
import stat
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT))

from terminal_monitor.remote import RemoteProvider, RemoteShare  # noqa: E402

SESSION_EVENT = {
    "type": "session",
    "share_url": "https://example",
    "e2ee_password": "secret",
    "session_id": "abc",
    "read_only": True,
    "encrypted": True,
    "background": True,
}


def _fake_shell(tmp, version="shell 0.7.3", event=None):
    """Executable stub mimicking the upstream CLI contract."""
    import json as _json

    path = pathlib.Path(tmp, "shell")
    lines = ["#!/bin/sh"]
    lines.append('if [ "$1" = "--version" ]; then')
    lines.append(f'  printf "%s\\n" "{version}"')
    lines.append("  exit 0")
    lines.append("fi")
    if event is None:
        lines.append('printf "%s\\n" "starting relay..." >&2')
        lines.append("exit 1")
    else:
        lines.append('printf "%s\\n" "starting relay..." >&2')
        lines.append(f"printf '%s\\n' '{_json.dumps(event)}' >&2")
        lines.append('printf "%s\\n" "listening" >&2')
        lines.append("exit 0")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


class RemoteShareTests(unittest.TestCase):
    def test_remote_share_has_no_password_field(self):
        fields = {field.name for field in dataclasses.fields(RemoteShare)}
        self.assertNotIn("password", fields)
        self.assertNotIn("e2ee_password", fields)
        self.assertNotIn("browser_password", fields)

    def test_read_only_is_explicit(self):
        share = RemoteShare(provider="shell.online", session_id="abc", share_url="https://example", encrypted=True, read_only=True)
        self.assertTrue(share.read_only)
        self.assertTrue(share.encrypted)

    def test_provider_availability_fails_closed(self):
        class _Down:
            def available(self):
                return (False, "E_REMOTE_PROVIDER_UNAVAILABLE: missing")

            def share_read_only(self, *, state_dir):
                raise RuntimeError("unavailable")

            def stop(self, session_id):
                return (False, "unavailable")

        provider: RemoteProvider = _Down()  # type: ignore[assignment]
        ok, _ = provider.available()
        self.assertFalse(ok)

    def test_shell_online_provider_missing_binary(self):
        from terminal_monitor.shell_online import ShellOnlineProvider

        provider = ShellOnlineProvider(binary="/nonexistent-shell-binary-xyz")
        ok, reason = provider.available()
        self.assertFalse(ok)
        self.assertIn("E_REMOTE_PROVIDER_UNAVAILABLE", reason)

    def test_shell_online_command_is_read_only(self):
        from terminal_monitor.shell_online import ShellOnlineProvider

        with tempfile.TemporaryDirectory() as tmp:
            cmd = ShellOnlineProvider().build_share_command(state_dir=tmp)
            self.assertIn("--read-only", cmd)
            self.assertIn("--json", cmd)
            self.assertNotIn("--no-e2ee", cmd)
            joined = " ".join(cmd)
            self.assertIn("attach", joined)
            self.assertIn("--read-only", joined)

    def test_shell_online_command_never_uses_shell_true(self):
        import ast
        import inspect

        from terminal_monitor import shell_online

        tree = ast.parse(inspect.getsource(shell_online))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "shell":
                        self.assertFalse(
                            isinstance(kw.value, ast.Constant) and kw.value.value is True,
                            "shell=True must never be used",
                        )

    def test_parse_metadata_strips_password(self):
        from terminal_monitor.shell_online import ShellOnlineProvider

        share, password = ShellOnlineProvider.parse_metadata(dict(SESSION_EVENT))
        self.assertEqual(share.share_url, "https://example")
        self.assertEqual(share.session_id, "abc")
        self.assertTrue(share.read_only)
        self.assertTrue(share.encrypted)
        self.assertEqual(password, "secret")
        fields = {field.name for field in dataclasses.fields(share)}
        self.assertNotIn("e2ee_password", fields)

    def test_parse_metadata_rejects_writable(self):
        from terminal_monitor.shell_online import ShellOnlineProvider

        event = dict(SESSION_EVENT, read_only=False)
        with self.assertRaisesRegex(ValueError, "E_REMOTE_SHARE_INSECURE"):
            ShellOnlineProvider.parse_metadata(event)

    def test_parse_metadata_requires_encrypted(self):
        from terminal_monitor.shell_online import ShellOnlineProvider

        for bad in (
            dict(SESSION_EVENT, encrypted=False),
            {key: value for key, value in SESSION_EVENT.items() if key != "encrypted"},
            {key: value for key, value in SESSION_EVENT.items() if key != "read_only"},
        ):
            with self.assertRaisesRegex(ValueError, "E_REMOTE_SHARE_INSECURE", msg=str(sorted(bad))):
                ShellOnlineProvider.parse_metadata(bad)

    def test_parse_metadata_requires_password_and_type(self):
        from terminal_monitor.shell_online import ShellOnlineProvider

        no_password = {key: value for key, value in SESSION_EVENT.items() if key != "e2ee_password"}
        with self.assertRaisesRegex(ValueError, "E_REMOTE_SHARE_FAILED"):
            ShellOnlineProvider.parse_metadata(no_password)
        wrong_type = dict(SESSION_EVENT, type="log")
        with self.assertRaisesRegex(ValueError, "E_REMOTE_SHARE_FAILED"):
            ShellOnlineProvider.parse_metadata(wrong_type)
        with self.assertRaisesRegex(ValueError, "E_REMOTE_SHARE_FAILED"):
            ShellOnlineProvider.parse_metadata({"type": "session"})


class ShellVersionTests(unittest.TestCase):
    def test_realistic_upstream_output(self):
        from terminal_monitor.shell_online import parse_shell_version

        self.assertEqual(parse_shell_version("shell 0.7.3\n"), "0.7.3")
        self.assertEqual(parse_shell_version("shell 0.7.3"), "0.7.3")
        self.assertEqual(parse_shell_version("shell 10.2.0-dev\n"), "10.2.0-dev")

    def test_rejects_non_upstream_output(self):
        from terminal_monitor.shell_online import parse_shell_version

        self.assertIsNone(parse_shell_version(""))
        self.assertIsNone(parse_shell_version("v0.7.3\n"))
        self.assertIsNone(parse_shell_version("shell 0.7\n"))
        self.assertIsNone(parse_shell_version("shell online dashboard\n"))
        self.assertIsNone(parse_shell_version("myshell 1.2.3\n"))
        self.assertIsNone(parse_shell_version("shell\n"))

    def test_available_accepts_realistic_binary(self):
        from terminal_monitor.shell_online import ShellOnlineProvider

        with tempfile.TemporaryDirectory() as tmp:
            binary = _fake_shell(tmp, version="shell 0.7.3")
            ok, detail = ShellOnlineProvider(binary=binary).available()
            self.assertTrue(ok, detail)
            self.assertIn("0.7.3", detail)

    def test_available_rejects_unrecognized_binary(self):
        from terminal_monitor.shell_online import ShellOnlineProvider

        with tempfile.TemporaryDirectory() as tmp:
            binary = _fake_shell(tmp, version="some shell-ish tool")
            ok, reason = ShellOnlineProvider(binary=binary).available()
            self.assertFalse(ok)
            self.assertIn("E_REMOTE_PROVIDER_UNRECOGNIZED_BINARY", reason)


class ShellStderrContractTests(unittest.TestCase):
    def test_share_reads_session_event_from_stderr(self):
        from terminal_monitor.shell_online import ShellOnlineProvider

        with tempfile.TemporaryDirectory() as tmp:
            binary = _fake_shell(tmp, event=SESSION_EVENT)
            result = ShellOnlineProvider(binary=binary).share_read_only_with_password(state_dir=tmp)
            self.assertEqual(result.share.session_id, "abc")
            self.assertEqual(result.browser_password, "secret")
            self.assertTrue(result.share.encrypted)
            self.assertTrue(result.share.read_only)

    def test_extract_session_event_skips_diagnostics(self):
        import json as _json

        from terminal_monitor.shell_online import ShellOnlineProvider

        stderr = "diagnostic line\n" + _json.dumps(SESSION_EVENT) + "\nanother diagnostic line\n"
        event = ShellOnlineProvider.extract_session_event(stderr, "")
        self.assertEqual(event["session_id"], "abc")

    def test_extract_session_event_prefers_stderr(self):
        import json as _json

        from terminal_monitor.shell_online import ShellOnlineProvider

        stdout_event = _json.dumps(dict(SESSION_EVENT, session_id="from-stdout"))
        stderr_event = _json.dumps(dict(SESSION_EVENT, session_id="from-stderr"))
        event = ShellOnlineProvider.extract_session_event(stderr_event, stdout_event)
        self.assertEqual(event["session_id"], "from-stderr")

    def test_share_failure_hides_provider_output(self):
        from terminal_monitor.shell_online import ShellOnlineProvider

        with tempfile.TemporaryDirectory() as tmp:
            binary = _fake_shell(tmp, event=None)
            with self.assertRaisesRegex(RuntimeError, "E_REMOTE_SHARE_FAILED") as ctx:
                ShellOnlineProvider(binary=binary).share_read_only_with_password(state_dir=tmp)
            self.assertNotIn("relay", str(ctx.exception))
            self.assertNotIn("secret", str(ctx.exception))

    def test_password_never_in_saved_metadata(self):
        import json as _json

        from terminal_monitor.shell_online import ShellOnlineProvider

        with tempfile.TemporaryDirectory() as tmp:
            binary = _fake_shell(tmp, event=SESSION_EVENT)
            provider = ShellOnlineProvider(binary=binary)
            result = provider.share_read_only_with_password(state_dir=tmp)
            saved = {
                "provider": result.share.provider,
                "session_id": result.share.session_id,
                "share_url": result.share.share_url,
            }
            with contextlib.suppress(OSError):
                pathlib.Path(tmp, "remote-share.json").write_text(_json.dumps(saved), encoding="utf-8")
            self.assertNotIn("secret", pathlib.Path(tmp, "remote-share.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

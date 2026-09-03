import dataclasses
import pathlib
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT))

from terminal_monitor.remote import RemoteProvider, RemoteShare  # noqa: E402


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
        import tempfile

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

        share, password = ShellOnlineProvider.parse_metadata(
            {
                "share_url": "https://example",
                "e2ee_password": "secret",
                "session_id": "abc",
                "read_only": True,
                "encrypted": True,
            }
        )
        self.assertEqual(share.share_url, "https://example")
        self.assertEqual(share.session_id, "abc")
        self.assertTrue(share.read_only)
        self.assertEqual(password, "secret")
        fields = {field.name for field in dataclasses.fields(share)}
        self.assertNotIn("e2ee_password", fields)

    def test_parse_metadata_rejects_writable(self):
        from terminal_monitor.shell_online import ShellOnlineProvider

        with self.assertRaises(ValueError):
            ShellOnlineProvider.parse_metadata(
                {"share_url": "https://example", "e2ee_password": "s", "session_id": "abc", "read_only": False, "encrypted": True}
            )


if __name__ == "__main__":
    unittest.main()

"""Fast in-process coverage for managed PTY, CLI surfaces, and providers."""
import contextlib
import json
import os
import pathlib
import runpy
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT))

from terminal_monitor import session_host as host_module  # noqa: E402
from terminal_monitor.cli import (  # noqa: E402
    _parse_agent_command,
    _resolve_attach_state_dir,
    _run_attach,
    _run_share,
    _run_terminate_session,
    _run_unshare,
    build_parser,
    config_from_args,
)
from terminal_monitor.config import MonitorConfig  # noqa: E402
from terminal_monitor.managed_pty import ManagedPTYBackend, ManagedSessionClient, managed_session_is_reconnectable  # noqa: E402
from terminal_monitor.session_host import (  # noqa: E402
    SessionHost,
    SessionHostConfig,
    _ensure_private_state_dir,
    _is_socket_path,
    _resolved_inside,
    build_arg_parser,
)
from terminal_monitor.session_protocol import PROTOCOL_VERSION, receive_message, send_message  # noqa: E402
from terminal_monitor.shell_online import ShellOnlineProvider, _looks_like_shell_online, _resolve_binary  # noqa: E402


def _wait_for(predicate, timeout=15.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError, RuntimeError, ConnectionError, PermissionError):
            pass
        time.sleep(interval)
    return None


class SessionHostUnitTests(unittest.TestCase):
    def test_resolved_inside_rejects_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                _resolved_inside(pathlib.Path(tmp), "../escape")
            ok = _resolved_inside(pathlib.Path(tmp), "session-token")
            self.assertTrue(str(ok).startswith(str(pathlib.Path(tmp).resolve())))

    def test_ensure_private_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "sub", "state")
            base = _ensure_private_state_dir(target)
            self.assertTrue(base.is_dir())
            self.assertEqual(stat.S_IMODE(base.stat().st_mode) & 0o777, 0o700)
            os.chmod(base, 0o777)
            with self.assertRaises(RuntimeError):
                _ensure_private_state_dir(str(base))

    def test_host_config_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                SessionHost(SessionHostConfig(session_id="s", command=(), cwd=tmp, state_dir=tmp))
            with self.assertRaises(ValueError):
                SessionHost(SessionHostConfig(session_id="s", command=("a", ""), cwd=tmp, state_dir=tmp))
            with self.assertRaises(ValueError):
                SessionHost(SessionHostConfig(session_id="s", command=("true",), cwd=tmp, state_dir=tmp, cols=0))
            with self.assertRaises(ValueError):
                SessionHost(SessionHostConfig(session_id="s", command=("true",), cwd=tmp, state_dir=tmp, replay_bytes=0))

    def test_arg_parser_and_main_errors(self):
        args = build_arg_parser().parse_args(
            ["--state-dir", "/tmp/x", "--cwd", "/tmp", "--command-json", '["echo","hi"]']
        )
        self.assertEqual(args.state_dir, "/tmp/x")
        self.assertEqual(host_module.main(["--state-dir", "/tmp/x", "--cwd", "/tmp", "--command-json", "not-json"]), 2)
        self.assertEqual(host_module.main(["--state-dir", "/tmp/x", "--cwd", "/tmp", "--command-json", "[]"]), 2)
        self.assertEqual(
            host_module.main(["--state-dir", "/tmp/x", "--cwd", "/tmp", "--command-json", '{"a":1}']),
            2,
        )

    def test_is_socket_path_false_for_regular_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            regular = pathlib.Path(tmp, "plain.txt")
            regular.write_text("hi", encoding="utf-8")
            self.assertFalse(_is_socket_path(regular))
            self.assertFalse(_is_socket_path(pathlib.Path(tmp, "missing")))

    def test_token_and_metadata_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = SessionHost(SessionHostConfig(session_id="meta1", command=("true",), cwd=tmp, state_dir=tmp))
            host._root_pid = 4242
            host._write_token()
            token_path = pathlib.Path(tmp, "session-token")
            self.assertTrue(token_path.is_file())
            self.assertEqual(stat.S_IMODE(token_path.stat().st_mode) & 0o777, 0o600)
            host._write_metadata("running")
            meta = json.loads(pathlib.Path(tmp, "managed-session.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["session_id"], "meta1")
            self.assertEqual(meta["root_pid"], 4242)
            self.assertEqual(meta["backend"], "pty")

    def test_terminate_group_without_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = SessionHost(SessionHostConfig(session_id="s", command=("true",), cwd=tmp, state_dir=tmp))
            host._root_pid = None
            host._terminate_group()
            self.assertTrue(host._child_exited.is_set())
            self.assertTrue(host._shutdown.is_set())

    def _connection_pair(self, tmp, **overrides):
        host = SessionHost(SessionHostConfig(session_id="s", command=("true",), cwd=tmp, state_dir=tmp))
        client_sock, host_sock = socket.socketpair()
        client_sock.settimeout(5.0)
        host_sock.settimeout(5.0)
        worker = threading.Thread(target=host._handle_connection, args=(host_sock,), daemon=True)
        worker.start()
        self.addCleanup(worker.join, 5.0)
        return host, client_sock, worker

    def test_auth_failure_generic(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, client_sock, _ = self._connection_pair(tmp)
            with contextlib.suppress(OSError):
                send_message(client_sock, {"version": PROTOCOL_VERSION, "token": "wrong", "op": "status"})
                resp = receive_message(client_sock)
                self.assertFalse(resp.get("ok"))
                self.assertIn("E_SESSION_AUTH_FAILED", str(resp.get("error")))
            client_sock.close()
            self.assertTrue(host.token)

    def test_bad_version_and_unknown_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, client_sock, _ = self._connection_pair(tmp)
            with contextlib.suppress(OSError):
                send_message(client_sock, {"version": 999, "token": host.token, "op": "status"})
                resp = receive_message(client_sock)
                self.assertFalse(resp.get("ok"))
            client_sock.close()
        with tempfile.TemporaryDirectory() as tmp:
            host, client_sock, _ = self._connection_pair(tmp)
            with contextlib.suppress(OSError):
                send_message(client_sock, {"version": PROTOCOL_VERSION, "token": host.token, "op": "bogus"})
                resp = receive_message(client_sock)
                self.assertFalse(resp.get("ok"))
            client_sock.close()

    def test_status_snapshot_send_resize_handlers(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = SessionHost(SessionHostConfig(session_id="s", command=("true",), cwd=tmp, state_dir=tmp))
            host._replay.append(b"hello")
            for op, extra in (
                ({"op": "status"}, None),
                ({"op": "snapshot", "limit_bytes": 10}, b"hello"),
                ({"op": "send", "data_b64": "!!!not-b64!!!"}, None),
                ({"op": "send", "data_b64": "eA=="}, None),
                ({"op": "resize", "cols": 0, "rows": 10}, None),
                ({"op": "resize", "cols": 80, "rows": 24}, None),
                ({"op": "terminate"}, None),
            ):
                client_sock, host_sock = socket.socketpair()
                client_sock.settimeout(5.0)
                host_sock.settimeout(5.0)
                worker = threading.Thread(target=host._handle_connection, args=(host_sock,), daemon=True)
                worker.start()
                try:
                    with contextlib.suppress(OSError):
                        send_message(client_sock, {"version": PROTOCOL_VERSION, "token": host.token, **op})
                        resp = receive_message(client_sock)
                        if extra is not None:
                            import base64

                            self.assertEqual(base64.b64decode(resp["data_b64"].encode()), extra)
                        else:
                            self.assertIn("ok", resp)
                finally:
                    client_sock.close()
                    worker.join(5.0)

    def test_stream_handler_after_exit(self):
        import base64

        with tempfile.TemporaryDirectory() as tmp:
            host = SessionHost(SessionHostConfig(session_id="s", command=("true",), cwd=tmp, state_dir=tmp))
            host._replay.append(b"bye")
            host._exit_code = 3
            host._child_exited.set()
            client_sock, host_sock = socket.socketpair()
            client_sock.settimeout(5.0)
            host_sock.settimeout(5.0)
            worker = threading.Thread(target=host._handle_connection, args=(host_sock,), daemon=True)
            worker.start()
            try:
                with contextlib.suppress(OSError):
                    send_message(client_sock, {"version": PROTOCOL_VERSION, "token": host.token, "op": "stream"})
                    first = receive_message(client_sock)
                    self.assertEqual(first.get("event"), "snapshot")
                    self.assertEqual(base64.b64decode(first["data_b64"].encode()), b"bye")
                    second = receive_message(client_sock)
                    self.assertEqual(second.get("event"), "exit")
                    self.assertEqual(second.get("exit_code"), 3)
            finally:
                client_sock.close()
                worker.join(5.0)


@unittest.skipIf(os.name != "posix", "managed PTY requires POSIX")
class InProcessHostRunTests(unittest.TestCase):
    def test_full_lifecycle_in_process(self):
        import base64

        child = (
            sys.executable,
            "-u",
            "-c",
            "import sys,time; print('READY', flush=True); time.sleep(8)",
        )
        with tempfile.TemporaryDirectory() as tmp:
            host = SessionHost(
                SessionHostConfig(
                    session_id="covtest",
                    command=child,
                    cwd=tmp,
                    state_dir=tmp,
                    cols=80,
                    rows=24,
                    replay_bytes=65536,
                )
            )
            results: list[int] = []
            thread = threading.Thread(target=lambda: results.append(host.run()), daemon=True)
            thread.start()
            try:
                client = _wait_for(
                    lambda: ManagedSessionClient.connect(tmp) if pathlib.Path(tmp, "session-control.sock").exists() else None,
                    timeout=15.0,
                )
                self.assertIsNotNone(client)
                assert client is not None
                self.addCleanup(self._quiet_close, client)
                self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
                status = client.status()
                self.assertTrue(status.alive)
                self.assertEqual(status.session_id, "covtest")
                client.send_bytes(b"hello\n")
                client.resize(100, 30)
                # Raw stream read: one snapshot event, then disconnect.
                token = pathlib.Path(tmp, "session-token").read_text(encoding="utf-8").strip()
                raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                raw.settimeout(10.0)
                try:
                    raw.connect(str(pathlib.Path(tmp, "session-control.sock")))
                    send_message(raw, {"version": PROTOCOL_VERSION, "token": token, "op": "stream"})
                    event = receive_message(raw)
                    self.assertEqual(event.get("event"), "snapshot")
                    self.assertIn(b"READY", base64.b64decode(event["data_b64"].encode()))
                finally:
                    raw.close()
                client.terminate_session()
                thread.join(timeout=20.0)
                self.assertFalse(thread.is_alive())
                self.assertEqual(results, [0])
                meta = json.loads(pathlib.Path(tmp, "managed-session.json").read_text(encoding="utf-8"))
                self.assertEqual(meta["state"], "exited")
            finally:
                with contextlib.suppress(OSError, ValueError, RuntimeError):
                    ManagedSessionClient(tmp, "x").terminate_session()
                thread.join(timeout=20.0)

    def _quiet_close(self, client):
        with contextlib.suppress(OSError, ValueError, RuntimeError, ConnectionError, PermissionError):
            client.terminate_session()
        with contextlib.suppress(OSError, ValueError):
            client.close()


class ManagedCLIUnitTests(unittest.TestCase):
    def test_parse_agent_command(self):
        self.assertEqual(_parse_agent_command("claude --model x", ()), ("claude", "--model", "x"))
        self.assertEqual(_parse_agent_command(["ignored"], ["from-file"]), ("from-file",))
        self.assertEqual(_parse_agent_command(None, ()), ())
        self.assertEqual(_parse_agent_command("", ()), ())
        with self.assertRaises(ValueError):
            _parse_agent_command(None, "not-a-list")
        with self.assertRaises(ValueError):
            _parse_agent_command(None, ["ok", ""])

    def test_parser_has_pty_and_subcommands(self):
        parser = build_parser()
        args = parser.parse_args(["supervise", "--backend", "pty", "--agent-command", "claude --flag"])
        self.assertEqual(args.backend, "pty")
        subparsers_actions = parser._subparsers._group_actions[0]
        for name in ("attach", "share", "unshare", "terminate-session"):
            self.assertIn(name, subparsers_actions.choices)

    def test_attach_requires_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = build_parser()
            args = parser.parse_args(["attach", "--state-dir", tmp])
            config = config_from_args(args)
            self.assertEqual(_run_attach(args, config), 2)

    def test_attach_missing_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = build_parser()
            args = parser.parse_args(["attach", "--state-dir", tmp, "--read-only"])
            config = config_from_args(args)
            self.assertEqual(_run_attach(args, config), 2)

    def test_share_unknown_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = build_parser()
            args = parser.parse_args(["share", "--provider", "nope", "--state-dir", tmp])
            config = config_from_args(args)
            self.assertEqual(_run_share(args, config), 2)

    def test_share_and_terminate_without_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = build_parser()
            args = parser.parse_args(["share", "--provider", "shell-online", "--state-dir", tmp])
            config = config_from_args(args)
            self.assertEqual(_run_share(args, config), 2)
            unshare_args = parser.parse_args(["unshare", "--state-dir", tmp])
            self.assertEqual(_run_unshare(unshare_args, config), 2)
            term_args = parser.parse_args(["terminate-session", "--state-dir", tmp])
            self.assertEqual(_run_terminate_session(term_args, config), 2)

    def test_resolve_state_dir_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = build_parser()
            args = parser.parse_args(["attach", "--state-dir", tmp, "--read-only"])
            config = config_from_args(args)
            self.assertEqual(_resolve_attach_state_dir(args, config), tmp)

    def test_remote_provider_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {
                "backend": "pty",
                "agent_command": ["claude"],
                "remote_provider": "shell-online",
            }
            pathlib.Path(tmp, ".terminal-monitor.json").write_text(json.dumps(cfg), encoding="utf-8")
            parser = build_parser()
            args = parser.parse_args(["supervise", "--project-dir", tmp])
            config = config_from_args(args)
            self.assertEqual(config.remote_provider, "shell-online")
            self.assertEqual(config.agent_command, ("claude",))


class ShellOnlineUnitTests(unittest.TestCase):
    def _script(self, tmp, body):
        path = pathlib.Path(tmp, "shell")
        path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        os.chmod(path, 0o700)
        return str(path)

    def test_looks_like(self):
        self.assertTrue(_looks_like_shell_online("shell.online v1.0"))
        self.assertFalse(_looks_like_shell_online("totally different tool"))

    def test_resolve_binary_explicit_missing(self):
        self.assertIsNone(_resolve_binary("/nonexistent-shell-binary-xyz"))

    def test_available_success_and_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = self._script(tmp, 'echo "shell.online v1.2.3"')
            ok, _ = ShellOnlineProvider(binary=good).available()
            self.assertTrue(ok)
        with tempfile.TemporaryDirectory() as tmp:
            other = self._script(tmp, 'echo "other tool"')
            ok, reason = ShellOnlineProvider(binary=other).available()
            self.assertFalse(ok)
            self.assertIn("UNRECOGNIZED", reason)
        with tempfile.TemporaryDirectory() as tmp:
            failing = self._script(tmp, "exit 3")
            ok, _ = ShellOnlineProvider(binary=failing).available()
            self.assertFalse(ok)

    def test_stop_success_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = self._script(
                tmp,
                'if [ "$1" = "--version" ]; then echo "shell.online v1"; elif [ "$1" = "kill" ]; then exit 0; else exit 1; fi',
            )
            ok, _ = ShellOnlineProvider(binary=script).stop("abc123")
            self.assertTrue(ok)
            ok, _ = ShellOnlineProvider(binary="/nonexistent-shell-xyz").stop("abc123")
            self.assertFalse(ok)

    def test_share_success_and_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = self._script(
                tmp,
                'if [ "$1" = "--version" ]; then echo "shell.online v1"; else '
                "echo '{\"share_url\":\"https://x\",\"session_id\":\"s1\",\"e2ee_password\":\"pw\",\"read_only\":true,\"encrypted\":true}'; fi",
            )
            result = ShellOnlineProvider(binary=script).share_read_only_with_password(state_dir=tmp)
            self.assertEqual(result.share.session_id, "s1")
            self.assertEqual(result.browser_password, "pw")
            self.assertTrue(result.share.read_only)
            with self.assertRaises(RuntimeError):
                ShellOnlineProvider(binary="/nonexistent-shell-xyz").share_read_only_with_password(state_dir=tmp)


class ManagedClientUnitTests(unittest.TestCase):
    def test_start_rejects_empty_command(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            ManagedSessionClient.start(state_dir=tmp, command=(), cwd=tmp)

    def test_connect_missing_token(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(FileNotFoundError):
            ManagedSessionClient.connect(tmp)

    def test_reconnectable_false_on_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(managed_session_is_reconnectable(tmp))

    def test_status_fallback_to_exited_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = {
                "schema_version": 1,
                "session_id": "gone",
                "backend": "pty",
                "host_pid": 1,
                "root_pid": 2,
                "command": ["true"],
                "cwd": tmp,
                "started_at": "2026-01-01T00:00:00Z",
                "state": "exited",
                "exit_code": 3,
            }
            pathlib.Path(tmp, "managed-session.json").write_text(json.dumps(meta), encoding="utf-8")
            client = ManagedSessionClient(tmp, "x")
            status = client.status()
            self.assertFalse(status.alive)
            self.assertEqual(status.exit_code, 3)

    def test_backend_without_state_dir_fails_closed(self):
        backend = ManagedPTYBackend()
        tab = backend.get_tab("x")
        self.assertFalse(tab.get("ok"))
        ok, _ = backend.send("x", None, "hi")
        self.assertFalse(ok)
        ok, _ = backend.send_key("x", None, "frobnicator-key")
        self.assertFalse(ok)
        self.assertEqual(backend.get_pids("x"), [])
        backend.close()

    def test_backend_aliases(self):
        import terminal_monitor

        self.assertEqual(terminal_monitor.get_backend("managed").name(), "pty")
        self.assertEqual(terminal_monitor.get_backend("managed-pty").name(), "pty")
        with self.assertRaises(ValueError) as ctx:
            terminal_monitor.get_backend("nope")
        self.assertIn("pty", str(ctx.exception))

    def test_monitor_remote_status_redacted(self):
        from terminal_monitor.monitor import TerminalMonitor

        with tempfile.TemporaryDirectory() as tmp:
            backend = mock.Mock()
            backend.owns_process = False
            backend.get_pids.return_value = []
            config = MonitorConfig(process="opencode", state_dir=tmp, project_dir=tmp)
            monitor = TerminalMonitor(config, backend=backend)
            remote = monitor._remote_share_status()
            self.assertFalse(remote.get("active"))
            pathlib.Path(tmp, "remote-share.json").write_text(
                json.dumps(
                    {
                        "provider": "shell.online",
                        "active": True,
                        "read_only": True,
                        "encrypted": True,
                        "session_id": "s",
                        "share_url": "u",
                        "e2ee_password": "secret",
                    }
                ),
                encoding="utf-8",
            )
            remote = monitor._remote_share_status()
            self.assertNotIn("e2ee_password", json.dumps(remote))
            runtime = monitor.managed_runtime_status()
            self.assertFalse(runtime.get("connected"))


    def test_monitor_adopts_existing_session_and_stop_keeps_agent(self):
        from terminal_monitor.monitor import TerminalMonitor

        child = (
            sys.executable,
            "-u",
            "-c",
            "import time; print('READY', flush=True); time.sleep(30)",
        )
        with tempfile.TemporaryDirectory() as tmp:
            first = ManagedSessionClient.start(state_dir=tmp, command=child, cwd=tmp)
            self.addCleanup(self._quiet_client, first)
            self.assertTrue(_wait_for(lambda: b"READY" in first.snapshot(), timeout=10.0))
            before = first.status()
            config = MonitorConfig(
                process="opencode",
                backend="pty",
                agent_command=child,
                state_dir=tmp,
                project_dir=tmp,
                web_ui=False,
                desktop_notifications=False,
            )
            monitor = TerminalMonitor(config)
            after = monitor.backend._client.status()
            self.assertEqual(before.session_id, after.session_id)
            self.assertEqual(before.root_pid, after.root_pid)
            monitor._stop_status("test_stop")
            time.sleep(0.3)
            self.assertTrue(first.status().alive)

    def _quiet_client(self, client):
        with contextlib.suppress(OSError, ValueError, RuntimeError, ConnectionError, PermissionError):
            client.terminate_session()
        with contextlib.suppress(OSError, ValueError):
            client.close()


class EntrypointTests(unittest.TestCase):
    def test_package_main_version(self):
        with mock.patch.object(sys, "argv", ["terminal_monitor", "--version"]):
            with self.assertRaises(SystemExit) as ctx:
                runpy.run_module("terminal_monitor.__main__", run_name="__main__")
            self.assertEqual(ctx.exception.code, 0)

    def test_supervise_main_with_mocked_monitor(self):
        from terminal_monitor import supervise

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(supervise, "TerminalMonitor") as mocked:
            mocked.return_value.run.return_value = 0
            code = supervise.main(["--state-dir", tmp, "--project-dir", tmp, "--no-web-ui", "--once"])
            self.assertEqual(code, 0)
            mocked.assert_called_once()


if __name__ == "__main__":
    unittest.main()

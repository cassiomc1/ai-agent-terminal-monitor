import contextlib
import json
import os
import pathlib
import shutil
import socket
import stat
import sys
import tempfile
import time
import unittest

REPO_ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT))


from terminal_monitor.managed_pty import ManagedSessionClient  # noqa: E402
from terminal_monitor.session_protocol import PROTOCOL_VERSION, SessionProtocolError, receive_message, send_message  # noqa: E402

CHILD_ECHO = (
    sys.executable,
    "-u",
    "-c",
    "import sys,time; print('READY', flush=True); "
    "line=sys.stdin.readline(); print('ECHO:'+line.strip(), flush=True); "
    "time.sleep(0.5)",
)


def _wait_for(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError, RuntimeError, SessionProtocolError, ConnectionError, PermissionError):
            pass
        time.sleep(interval)
    return None


def _pid_dead(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        return True
    return False


@unittest.skipIf(os.name != "posix", "managed PTY requires POSIX")
class SessionHostTests(unittest.TestCase):
    def _state_dir(self):
        """State dir that outlives client cleanup (cleanups run LIFO).

        A ``with TemporaryDirectory()`` block would delete the control socket
        before teardown runs, so the session could no longer be terminated and
        the detached host would survive the test run.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return tmp

    def _start(self, tmp, command=CHILD_ECHO):
        client = ManagedSessionClient.start(state_dir=tmp, command=command, cwd=tmp)
        self.addCleanup(self._cleanup_client, client, tmp)
        return client

    def _cleanup_client(self, client, tmp):
        with contextlib.suppress(OSError, ValueError, RuntimeError, SessionProtocolError, ConnectionError, PermissionError):
            client.terminate_session()
        with contextlib.suppress(OSError, ValueError, RuntimeError):
            client.close()
        # Deterministic teardown: reap the host and leave no orphan agent.
        try:
            meta = json.loads(pathlib.Path(tmp, "managed-session.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        for pid in (meta.get("host_pid"), meta.get("root_pid")):
            if isinstance(pid, int) and pid > 0 and not _wait_for(lambda pid=pid: _pid_dead(pid), timeout=10.0):
                with contextlib.suppress(OSError, ValueError):
                    try:
                        os.killpg(pid, 9)
                    except (ProcessLookupError, PermissionError, OSError):
                        with contextlib.suppress(OSError):
                            os.kill(pid, 9)
                _wait_for(lambda pid=pid: _pid_dead(pid), timeout=10.0)

    def test_pty_lifecycle_ready_echo_exit(self):
        tmp = self._state_dir()
        client = self._start(tmp)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        status = client.status()
        self.assertTrue(status.alive)
        self.assertGreater(status.host_pid, 0)
        self.assertGreater(status.root_pid, 0)
        client.send_bytes(b"hello\n")
        self.assertTrue(_wait_for(lambda: b"ECHO:hello" in client.snapshot(), timeout=10.0))
        # Child exits ~0.5s after echo; exit becomes observable.
        self.assertTrue(_wait_for(lambda: client.status().exit_code is not None or not client.status().alive, timeout=10.0))

    def test_invalid_token_is_rejected(self):
        tmp = self._state_dir()
        client = self._start(tmp)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        meta = json.loads((pathlib.Path(tmp) / "managed-session.json").read_text())
        self.assertIn("session_id", meta)
        sock_path = str(pathlib.Path(tmp) / "session-control.sock")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        try:
            sock.connect(sock_path)
            send_message(sock, {"version": PROTOCOL_VERSION, "token": "wrong-token", "op": "status"})
            resp = receive_message(sock)
        finally:
            sock.close()
        self.assertFalse(resp.get("ok"))
        self.assertIn("E_SESSION_AUTH_FAILED", str(resp.get("error", "")))
        # Host still healthy after auth failure.
        self.assertTrue(client.status().alive)

    def test_oversized_control_message_does_not_kill_host(self):
        import threading

        tmp = self._state_dir()
        client = self._start(tmp)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        sock_path = str(pathlib.Path(tmp) / "session-control.sock")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        payload = b"x" * (64 * 1024 + 1024) + b"\n"

        def _send() -> None:
            with contextlib.suppress(OSError):
                sock.sendall(payload)

        sender = threading.Thread(target=_send, daemon=True)
        try:
            sock.connect(sock_path)
            # Send >64KiB: host must fail the client, never itself.
            sender.start()
            try:
                resp = receive_message(sock)
                # Either host closes (protocol error) or returns error envelope.
                self.assertFalse(resp.get("ok", True))
            except SessionProtocolError:
                pass
            sender.join(timeout=5.0)
        finally:
            sock.close()
        # Authenticated operation still works; host survived.
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=5.0))

    def test_send_payload_limit(self):
        tmp = self._state_dir()
        client = self._start(tmp)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        with self.assertRaises(ValueError):
            client.send_bytes(b"x" * (64 * 1024 + 1))
        self.assertTrue(client.status().alive)

    def test_resize_limits(self):
        tmp = self._state_dir()
        client = self._start(tmp)
        client.resize(80, 24)
        with self.assertRaises(ValueError):
            client.resize(0, 24)
        with self.assertRaises(ValueError):
            client.resize(80, 0)
        with self.assertRaises(ValueError):
            client.resize(5000, 24)

    def test_state_dir_and_token_permissions(self):
        tmp = self._state_dir()
        self._start(tmp)
        base = pathlib.Path(tmp)
        mode = stat.S_IMODE(base.stat().st_mode)
        self.assertEqual(mode & 0o777, 0o700)
        token_mode = stat.S_IMODE((base / "session-token").stat().st_mode)
        self.assertEqual(token_mode & 0o777, 0o600)
        sock_mode = stat.S_IMODE((base / "session-control.sock").stat().st_mode)
        self.assertEqual(sock_mode & 0o777, 0o600)

    def test_disconnect_resilience_same_session(self):
        long_child = (
            sys.executable,
            "-u",
            "-c",
            "import sys,time; print('READY', flush=True); "
            "import time as _t; [print(f'TICK{i}', flush=True) or _t.sleep(0.2) for i in range(30)]",
        )
        tmp = self._state_dir()
        client = self._start(tmp, command=long_child)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        first = client.status()
        client.close()
        # Produce output while supervisor is disconnected, then reconnect.
        time.sleep(0.6)
        client2 = ManagedSessionClient.connect(tmp)
        self.addCleanup(self._cleanup_client, client2, tmp)
        second = client2.status()
        self.assertEqual(first.session_id, second.session_id)
        self.assertEqual(first.root_pid, second.root_pid)
        self.assertTrue(second.alive)
        snap = client2.snapshot()
        self.assertIn(b"READY", snap)
        # Replay includes output produced while disconnected.
        self.assertTrue(b"TICK" in snap)

    def test_terminate_kills_process_group(self):
        tmp = self._state_dir()
        client = self._start(tmp)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        root = client.status().root_pid
        self.assertTrue(root > 0)
        client.terminate_session()

        def _gone():
            try:
                os.kill(root, 0)
                return False
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            except OSError:
                return True

        self.assertTrue(_wait_for(_gone, timeout=10.0))


if __name__ == "__main__":
    unittest.main()

"""P0/P1 hardening tests: ownership, concurrency, termination, drain, redaction."""
import base64
import contextlib
import gc
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT))

from terminal_monitor.managed_pty import ManagedSessionClient, managed_session_is_reconnectable  # noqa: E402
from terminal_monitor.session_host import SessionHost, SessionHostConfig, classify_startup_state  # noqa: E402
from terminal_monitor.session_protocol import (  # noqa: E402
    MAX_CONTROL_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    SessionProtocolError,
    send_message,
)

LONG_CHILD = (
    sys.executable,
    "-u",
    "-c",
    "import time; print('READY', flush=True); time.sleep(60)",
)
ECHO_CHILD = (
    sys.executable,
    "-u",
    "-c",
    "import sys,time; print('READY', flush=True); "
    "line=sys.stdin.readline(); print('ECHO:'+line.strip(), flush=True); "
    "time.sleep(30)",
)
IGNORING_CHILD = (
    sys.executable,
    "-u",
    "-c",
    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "print('READY', flush=True); time.sleep(60)",
)
EXITING_CHILD = (
    sys.executable,
    "-u",
    "-c",
    # Startup requires a live child, so exit shortly after becoming ready
    # instead of immediately.
    "import time; print('READY', flush=True); time.sleep(2)",
)


def _wait_for(predicate, timeout=15.0, interval=0.05):
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


def _read_meta(tmp):
    try:
        data = json.loads(pathlib.Path(tmp, "managed-session.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class StateDirMixin:
    """Provides state dirs that outlive managed-session teardown."""

    def _state_dir(self):
        """Fresh state dir whose removal is registered before any client cleanup.

        unittest runs cleanups LIFO, so registering the directory first makes it
        outlive every client cleanup added afterwards. A ``with
        TemporaryDirectory()`` block instead deletes the control socket while the
        host is still running, leaving teardown unable to terminate or reap it:
        every ``terminate_session`` then burns its full 15s reap grace and leaks
        a live host plus agent.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return tmp


@unittest.skipIf(os.name != "posix", "managed PTY requires POSIX")
class ManagedTestBase(StateDirMixin, unittest.TestCase):
    def _start(self, tmp, command=LONG_CHILD):
        client = ManagedSessionClient.start(state_dir=tmp, command=command, cwd=tmp)
        self.addCleanup(self._cleanup_client, client, tmp)
        return client

    def _cleanup_client(self, client, tmp):
        with contextlib.suppress(OSError, ValueError, RuntimeError, SessionProtocolError, ConnectionError, PermissionError):
            client.terminate_session()
        with contextlib.suppress(OSError, ValueError, RuntimeError):
            client.close()
        # Deterministic teardown: no orphan host or agent may survive tests.
        meta = _read_meta(tmp)
        for pid in (meta.get("host_pid"), meta.get("root_pid")):
            if isinstance(pid, int) and pid > 0 and not _wait_for(lambda pid=pid: _pid_dead(pid), timeout=10.0):
                with contextlib.suppress(OSError, ValueError):
                    try:
                        os.killpg(pid, 9)
                    except (ProcessLookupError, PermissionError, OSError):
                        with contextlib.suppress(OSError):
                            os.kill(pid, 9)
                _wait_for(lambda pid=pid: _pid_dead(pid), timeout=10.0)


class OwnershipTests(ManagedTestBase):
    def test_healthy_host_is_adopted_not_duplicated(self):
        tmp = self._state_dir()
        first = self._start(tmp)
        self.assertTrue(_wait_for(lambda: b"READY" in first.snapshot(), timeout=10.0))
        before = first.status()
        second = ManagedSessionClient.start(state_dir=tmp, command=LONG_CHILD, cwd=tmp)
        self.addCleanup(self._cleanup_client, second, tmp)
        after = second.status()
        self.assertEqual(before.session_id, after.session_id)
        self.assertEqual(before.root_pid, after.root_pid)
        self.assertTrue(managed_session_is_reconnectable(tmp))

    def test_dead_pid_allows_stale_takeover(self):
        tmp = self._state_dir()
        proc = subprocess.Popen(["true"])
        dead_pid = proc.pid
        proc.wait(timeout=10)
        self.assertTrue(_pid_dead(dead_pid))
        pathlib.Path(tmp).mkdir(parents=True, exist_ok=True)
        pathlib.Path(tmp, "managed-session.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": "old",
                    "backend": "pty",
                    "host_pid": dead_pid,
                    "root_pid": dead_pid,
                    "cwd": tmp,
                    "started_at": "2026-01-01T00:00:00Z",
                    "state": "running",
                    "exit_code": None,
                }
            ),
            encoding="utf-8",
        )
        decision, _ = classify_startup_state(tmp)
        self.assertEqual(decision, "stale")
        client = self._start(tmp)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        self.assertNotEqual(client.status().session_id, "old")

    def test_live_pid_missing_socket_fails_closed(self):
        tmp = self._state_dir()
        client = ManagedSessionClient.start(state_dir=tmp, command=LONG_CHILD, cwd=tmp)
        self.addCleanup(self._cleanup_client, client, tmp)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        before = client.status()
        sock = pathlib.Path(tmp, "session-control.sock")
        hidden = pathlib.Path(tmp, "session-control.sock.hidden")
        os.rename(sock, hidden)
        self.addCleanup(self._restore_path, hidden, sock)
        with self.assertRaisesRegex(RuntimeError, "E_SESSION_OWNERSHIP_UNCERTAIN"):
            ManagedSessionClient.start(state_dir=tmp, command=LONG_CHILD, cwd=tmp)
        # No replacement agent: original host and agent PIDs still alive.
        self.assertFalse(_pid_dead(before.host_pid))
        self.assertFalse(_pid_dead(before.root_pid))

    def _restore_path(self, hidden, sock):
        with contextlib.suppress(OSError):
            if hidden.exists() and not sock.exists():
                os.rename(hidden, sock)

    def test_live_pid_invalid_token_fails_closed(self):
        tmp = self._state_dir()
        client = self._start(tmp)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        before = client.status()
        token_path = pathlib.Path(tmp, "session-token")
        saved = token_path.read_text(encoding="utf-8")
        token_path.write_text("bogus-token\n", encoding="utf-8")
        try:
            with self.assertRaisesRegex(RuntimeError, "E_SESSION_OWNERSHIP_UNCERTAIN"):
                ManagedSessionClient.start(state_dir=tmp, command=LONG_CHILD, cwd=tmp)
            self.assertFalse(_pid_dead(before.host_pid))
            self.assertFalse(_pid_dead(before.root_pid))
        finally:
            token_path.write_text(saved, encoding="utf-8")
        # Restored auth reaches the very same session: nothing replaced.
        live = client.status()
        self.assertEqual(before.session_id, live.session_id)
        self.assertTrue(live.alive)

    def test_live_pid_unavailable_socket_fails_closed(self):
        tmp = self._state_dir()
        client = self._start(tmp)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        before = client.status()
        sock = pathlib.Path(tmp, "session-control.sock")
        sock.chmod(0o000)
        try:
            with self.assertRaisesRegex(RuntimeError, "E_SESSION_OWNERSHIP_UNCERTAIN"):
                ManagedSessionClient.start(state_dir=tmp, command=LONG_CHILD, cwd=tmp)
            self.assertFalse(_pid_dead(before.host_pid))
            self.assertFalse(_pid_dead(before.root_pid))
        finally:
            sock.chmod(0o600)
        live = client.status()
        self.assertEqual(live.session_id, before.session_id)
        self.assertTrue(live.alive)

    def test_unparseable_metadata_is_uncertain(self):
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, "managed-session.json").write_text("{not json", encoding="utf-8")
            decision, _ = classify_startup_state(tmp)
            self.assertEqual(decision, "uncertain")
            with self.assertRaisesRegex(RuntimeError, "E_SESSION_OWNERSHIP_UNCERTAIN"):
                ManagedSessionClient.start(state_dir=tmp, command=LONG_CHILD, cwd=tmp)

    def test_absent_metadata_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            decision, _ = classify_startup_state(tmp)
            self.assertEqual(decision, "absent")


class ConcurrentStartupTests(ManagedTestBase):
    def test_simultaneous_starts_yield_one_agent(self):
        tmp = self._state_dir()
        barrier = threading.Barrier(3)
        outcomes: list = []
        errors: list = []

        def _race():
            try:
                barrier.wait(timeout=10)
                outcomes.append(ManagedSessionClient.start(state_dir=tmp, command=LONG_CHILD, cwd=tmp))
            except (OSError, ValueError, RuntimeError) as exc:
                errors.append(exc)

        workers = [threading.Thread(target=_race, daemon=True) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=10)
        for worker in workers:
            worker.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), 2)
        for outcome in outcomes:
            self.addCleanup(self._cleanup_client, outcome, tmp)
        first, second = (outcome.status() for outcome in outcomes)
        self.assertEqual(first.session_id, second.session_id)
        self.assertEqual(first.root_pid, second.root_pid)
        # Exactly one agent process for the directory.
        self.assertTrue(first.alive)


class TerminateTests(ManagedTestBase):
    def test_terminate_acknowledges_running_child(self):
        tmp = self._state_dir()
        client = self._start(tmp, command=ECHO_CHILD)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        root = client.status().root_pid
        own_pgid = os.getpgid(0)
        child_pgid = os.getpgid(root)
        self.assertNotEqual(child_pgid, own_pgid)
        # Must return a valid response (Linux race regression).
        client.terminate_session()
        self.assertTrue(_wait_for(lambda: _pid_dead(root), timeout=10.0))
        with self.assertRaises((ProcessLookupError, PermissionError, OSError)):
            os.killpg(child_pgid, 0)
        # The test process itself is untouched.
        self.assertEqual(os.getpgid(0), own_pgid)

    def test_terminate_sigkill_after_grace(self):
        tmp = self._state_dir()
        client = self._start(tmp, command=IGNORING_CHILD)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        root = client.status().root_pid
        started = time.monotonic()
        client.terminate_session()
        self.assertTrue(_wait_for(lambda: _pid_dead(root), timeout=15.0))
        self.assertGreaterEqual(time.monotonic() - started, 4.0)

    def test_terminate_idempotent_after_exit(self):
        tmp = self._state_dir()
        client = self._start(tmp, command=EXITING_CHILD)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        self.assertTrue(
            _wait_for(lambda: not client.status().alive, timeout=15.0),
            "expected the child to exit",
        )
        client.terminate_session()
        client.terminate_session()

    def test_no_unreaped_host_process(self):
        with tempfile.TemporaryDirectory() as tmp, warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            client = ManagedSessionClient.start(state_dir=tmp, command=ECHO_CHILD, cwd=tmp)
            self.assertTrue(_wait_for(lambda client=client: b"READY" in client.snapshot(), timeout=10.0))
            client.terminate_session()
            client.close()
            del client
            gc.collect()
        leftovers = [warning for warning in caught if "still running" in str(warning.message)]
        self.assertEqual(leftovers, [])


class FinalizeCompletionTests(StateDirMixin, unittest.TestCase):
    """``run()`` must not return before the final state is durable.

    ``_finalize`` runs in whichever thread observes the exit (reader, reaper,
    or the terminate worker), while ``run()`` returns as soon as the accept
    loop sees the session ended. If the second caller returned early instead
    of waiting, the process could exit mid-finalize and leave ``state:
    running`` with no exit code on disk — an exit no client can ever observe.
    """

    def _run_host(self, command, session_id="finaltest"):
        tmp = self._state_dir()
        host = SessionHost(
            SessionHostConfig(
                session_id=session_id,
                command=command,
                cwd=tmp,
                state_dir=tmp,
                cols=80,
                rows=24,
                replay_bytes=65536,
            )
        )
        results = []
        thread = threading.Thread(target=lambda: results.append(host.run()), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 20.0)
        return tmp, host, thread, results

    def test_metadata_is_final_when_run_returns(self):
        brief = (sys.executable, "-u", "-c", "import time; print('READY', flush=True); time.sleep(1)")
        tmp, _host, thread, results = self._run_host(brief)
        thread.join(timeout=30.0)
        self.assertFalse(thread.is_alive(), "host thread did not exit after the child exited")
        self.assertEqual(results, [0])
        # No polling: the invariant is that finalization already completed.
        meta = _read_meta(tmp)
        self.assertEqual(meta.get("state"), "exited")
        self.assertEqual(meta.get("exit_code"), 0)

    def test_terminate_records_exit_code_not_stopped(self):
        tmp, _host, thread, results = self._run_host(LONG_CHILD)
        client = _wait_for(
            lambda: ManagedSessionClient.connect(tmp) if pathlib.Path(tmp, "session-control.sock").exists() else None,
            timeout=15.0,
        )
        self.assertIsNotNone(client)
        assert client is not None
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        client.terminate_session()
        thread.join(timeout=30.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [0])
        meta = _read_meta(tmp)
        # The reaper hands the exit code over before shutdown forces finalize,
        # so a killed child is never recorded as "stopped" with no exit code.
        self.assertEqual(meta.get("state"), "exited")
        self.assertIsNotNone(meta.get("exit_code"))


class DrainTests(ManagedTestBase):
    def _collect_stream(self, tmp, token, delay=0.0, after_connect=None):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(15.0)
        collected = bytearray()
        try:
            sock.connect(str(pathlib.Path(tmp, "session-control.sock")))
            send_message(sock, {"version": PROTOCOL_VERSION, "token": token, "op": "stream"})
            from terminal_monitor.session_protocol import FramedReader

            if after_connect is not None:
                after_connect()
            if delay:
                # Read nothing for a while so the host's per-viewer backlog fills.
                time.sleep(delay)
            reader = FramedReader(sock)
            first = reader.read_message()
            self.assertEqual(first.get("event"), "snapshot_start")
            while True:
                event = reader.read_message()
                if event.get("event") == "snapshot_end":
                    break
                if event.get("event") != "snapshot_chunk":
                    break
                collected += base64.b64decode(event["data_b64"].encode("ascii"))
            while True:
                event = reader.read_message()
                kind = event.get("event")
                if kind == "output":
                    collected += base64.b64decode(event["data_b64"].encode("ascii"))
                elif kind == "exit":
                    break
                else:
                    break
        finally:
            sock.close()
        return bytes(collected)

    def test_final_marker_survives_exit(self):
        for _ in range(5):
            child = (
                sys.executable,
                "-u",
                "-c",
                "import sys,time; time.sleep(1.0); "
                "[print(f'line{i}', flush=True) for i in range(500)]; print('FINAL_MARKER', flush=True)",
            )
            tmp = self._state_dir()
            client = ManagedSessionClient.start(state_dir=tmp, command=child, cwd=tmp)
            self.addCleanup(self._cleanup_client, client, tmp)
            token = pathlib.Path(tmp, "session-token").read_text(encoding="utf-8").strip()
            data = self._collect_stream(tmp, token)
            self.assertIn(b"FINAL_MARKER", data)

    def test_slow_viewer_keeps_tail_and_exit_event(self):
        """A viewer that falls behind must still get the tail and the exit event.

        Many small writes overflow the per-viewer item queue. Dropping the
        viewer there truncated the stream mid-frame with no exit event; the
        backlog is coalesced instead, so the tail survives.
        """
        child = (
            sys.executable,
            "-u",
            "-c",
            "import sys; print('READY', flush=True); sys.stdin.readline(); "
            "[print(f'row{i}', flush=True) for i in range(4000)]; "
            "print('TAIL_MARKER', flush=True)",
        )
        tmp = self._state_dir()
        client = ManagedSessionClient.start(state_dir=tmp, command=child, cwd=tmp)
        self.addCleanup(self._cleanup_client, client, tmp)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        token = pathlib.Path(tmp, "session-token").read_text(encoding="utf-8").strip()
        # Start the burst only once the viewer is attached, then read nothing
        # for long enough that the per-viewer queue overflows.
        data = self._collect_stream(tmp, token, delay=1.5, after_connect=lambda: client.send_bytes(b"go\n"))
        self.assertIn(b"TAIL_MARKER", data)

    def test_large_final_burst_drains(self):
        child = (
            sys.executable,
            "-u",
            "-c",
            "import sys,time; time.sleep(1.0); sys.stdout.write('B'*300*1024); "
            "sys.stdout.flush(); print('BURST_END', flush=True)",
        )
        tmp = self._state_dir()
        client = ManagedSessionClient.start(state_dir=tmp, command=child, cwd=tmp)
        self.addCleanup(self._cleanup_client, client, tmp)
        token = pathlib.Path(tmp, "session-token").read_text(encoding="utf-8").strip()
        data = self._collect_stream(tmp, token)
        self.assertIn(b"BURST_END", data)
        self.assertGreater(len(data), 300 * 1024)


class SnapshotChunkTests(ManagedTestBase):
    def _child_writing(self, size, marker):
        return (
            sys.executable,
            "-u",
            "-c",
            f"import sys,time; sys.stdout.write('Q'*{size}); sys.stdout.flush(); "
            f"print('{marker}', flush=True); time.sleep(30)",
        )

    def test_chunked_sizes_byte_equal(self):
        for size in (1024, 48 * 1024, 70 * 1024, 150 * 1024):
            with self.subTest(size=size):
                tmp = self._state_dir()
                marker = f"MARK_{size}".encode()
                client = self._start(tmp, command=self._child_writing(size, marker.decode()))

                def _ready(client=client, marker=marker):
                    data = client.snapshot()
                    return data if marker in data else None

                data = _wait_for(_ready, timeout=15.0)
                self.assertIsNotNone(data)
                assert data is not None
                self.assertIn(marker, data)
                self.assertGreaterEqual(len(data), size)

    def test_full_replay_capacity(self):
        tmp = self._state_dir()
        client = self._start(tmp, command=self._child_writing(600 * 1024, "FULLMARK"))
        data = _wait_for(lambda: client.snapshot() if b"FULLMARK" in client.snapshot() else None, timeout=20.0)
        self.assertIsNotNone(data)
        assert data is not None
        # Bounded to capacity: newest bytes win.
        self.assertLessEqual(len(data), 512 * 1024 + 4096)
        self.assertIn(b"FULLMARK", data)

    def test_frames_stay_within_budget(self):
        tmp = self._state_dir()
        client = self._start(tmp, command=self._child_writing(200 * 1024, "FRAMEMARK"))
        self.assertTrue(_wait_for(lambda: b"FRAMEMARK" in client.snapshot(), timeout=15.0))
        token = pathlib.Path(tmp, "session-token").read_text(encoding="utf-8").strip()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(15.0)
        try:
            sock.connect(str(pathlib.Path(tmp, "session-control.sock")))
            send_message(sock, {"version": PROTOCOL_VERSION, "token": token, "op": "snapshot", "limit_bytes": 512 * 1024, "chunked": True})
            buf = bytearray()
            frames = 0
            saw_end = False
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline and not saw_end:
                piece = sock.recv(65536)
                if not piece:
                    break
                buf += piece
                while b"\n" in buf:
                    line, _, rest = buf.partition(b"\n")
                    buf = bytearray(rest)
                    frames += 1
                    self.assertLessEqual(len(line) + 1, MAX_CONTROL_MESSAGE_BYTES + 1)
                    try:
                        event = json.loads(line.decode("utf-8"))
                    except ValueError:
                        continue
                    if event.get("event") == "snapshot_end":
                        saw_end = True
                        break
            self.assertTrue(saw_end, "expected a snapshot_end frame")
            self.assertGreater(frames, 3, "expected multiple chunk frames")
        finally:
            sock.close()

    def test_malformed_sequences_fail_closed(self):
        from terminal_monitor.managed_pty import _read_chunked_snapshot
        from terminal_monitor.session_protocol import FramedReader

        def _assemble(messages, close_early=False):
            s1, s2 = socket.socketpair()
            try:
                for message in messages:
                    s1.sendall(message)
                if close_early:
                    s1.close()
                    s1 = None  # type: ignore[assignment]
                with self.assertRaises(SessionProtocolError):
                    _read_chunked_snapshot(FramedReader(s2))
            finally:
                if s1 is not None:
                    s1.close()
                s2.close()

        import json as _json

        def _frame(payload):
            return (_json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode()

        good_chunk = _frame({"event": "snapshot_chunk", "seq": 0, "data_b64": base64.b64encode(b"hi").decode()})
        # Wrong first event.
        _assemble([_frame({"event": "output", "data_b64": ""})])
        # Out-of-order seq.
        _assemble([_frame({"event": "snapshot_start", "total_bytes": 2}), _frame({"event": "snapshot_chunk", "seq": 1, "data_b64": "eA=="})])
        # Truncated after start.
        _assemble([_frame({"event": "snapshot_start", "total_bytes": 2})], close_early=True)
        # Total mismatch at end.
        _assemble(
            [
                _frame({"event": "snapshot_start", "total_bytes": 5}),
                good_chunk,
                _frame({"event": "snapshot_end", "total_bytes": 5}),
            ]
        )
        # Bad base64.
        _assemble(
            [
                _frame({"event": "snapshot_start", "total_bytes": 2}),
                _frame({"event": "snapshot_chunk", "seq": 0, "data_b64": "!!!"}),
            ]
        )


class RedactionTests(ManagedTestBase):
    def test_command_secrets_not_persisted(self):
        secret = "super-secret-value-xyz-123"
        command = (sys.executable, "-u", "-c", "import time; print('READY', flush=True); time.sleep(30)", "--token", secret)
        tmp = self._state_dir()
        client = self._start(tmp, command=command)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        meta_text = pathlib.Path(tmp, "managed-session.json").read_text(encoding="utf-8")
        self.assertNotIn(secret, meta_text)
        self.assertNotIn("--token " + secret, meta_text)
        meta = json.loads(meta_text)
        self.assertNotIn("command", meta)
        self.assertIn("executable", meta)
        self.assertNotIn(secret, str(meta.get("command_display", "")))
        status = client.status()
        self.assertNotIn(secret, str(status))


class RemoteLifecycleTests(StateDirMixin, unittest.TestCase):
    def test_create_share_stops_previous_first(self):
        from terminal_monitor.cli import _create_share
        from terminal_monitor.remote import RemoteShare
        from terminal_monitor.shell_online import ShellOnlineLaunchResult

        calls: list = []

        class _FakeProvider:
            def stop(self, session_id):
                calls.append(("stop", session_id))
                return (True, "stopped")

            def share_read_only_with_password(self, *, state_dir):
                calls.append(("share", state_dir))
                return ShellOnlineLaunchResult(
                    share=RemoteShare(provider="shell.online", session_id="new", share_url="https://new", encrypted=True, read_only=True),
                    browser_password="pw-new",
                )

        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, "remote-share.json").write_text(
                json.dumps({"provider": "shell.online", "active": True, "read_only": True, "session_id": "old"}),
                encoding="utf-8",
            )
            result = _create_share(_FakeProvider(), tmp)
            self.assertEqual([call[0] for call in calls], ["stop", "share"])
            self.assertEqual(calls[0][1], "old")
            self.assertEqual(result.browser_password, "pw-new")
            saved = json.loads(pathlib.Path(tmp, "remote-share.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["session_id"], "new")
            self.assertNotIn("pw-new", json.dumps(saved))

    def test_create_share_without_previous(self):
        from terminal_monitor.cli import _create_share
        from terminal_monitor.remote import RemoteShare
        from terminal_monitor.shell_online import ShellOnlineLaunchResult

        class _FakeProvider:
            def stop(self, session_id):
                raise AssertionError("stop must not be called without a previous share")

            def share_read_only_with_password(self, *, state_dir):
                return ShellOnlineLaunchResult(
                    share=RemoteShare(provider="shell.online", session_id="s", share_url="https://u", encrypted=True, read_only=True),
                    browser_password="pw",
                )

        with tempfile.TemporaryDirectory() as tmp:
            result = _create_share(_FakeProvider(), tmp)
            self.assertEqual(result.share.session_id, "s")

    @unittest.skipIf(os.name != "posix", "managed PTY requires POSIX")
    def test_unshare_keeps_agent_alive(self):
        from terminal_monitor.cli import _run_unshare
        from terminal_monitor.shell_online import ShellOnlineProvider

        tmp = self._state_dir()
        client = ManagedSessionClient.start(state_dir=tmp, command=LONG_CHILD, cwd=tmp)
        self.addCleanup(self._cleanup_client, client, tmp)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        pathlib.Path(tmp, "remote-share.json").write_text(
            json.dumps({"provider": "shell.online", "active": True, "read_only": True, "session_id": "old-share"}),
            encoding="utf-8",
        )

        def _fake_stop(self, session_id):
            return (True, "stopped")

        with mock.patch.object(ShellOnlineProvider, "stop", _fake_stop):
            import argparse

            args = argparse.Namespace(state_dir=tmp, project_dir=tmp, session_id=None)
            config = argparse.Namespace(state_dir=tmp, project_dir=tmp)
            code = _run_unshare(args, config)
        self.assertEqual(code, 0)
        saved = json.loads(pathlib.Path(tmp, "remote-share.json").read_text(encoding="utf-8"))
        self.assertFalse(saved.get("active"))
        self.assertTrue(client.status().alive)

    def _cleanup_client(self, client, tmp):
        with contextlib.suppress(OSError, ValueError, RuntimeError, SessionProtocolError, ConnectionError, PermissionError):
            client.terminate_session()
        with contextlib.suppress(OSError, ValueError, RuntimeError):
            client.close()


if __name__ == "__main__":
    unittest.main()

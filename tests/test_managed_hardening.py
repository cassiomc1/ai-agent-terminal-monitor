"""P0/P1 hardening tests: ownership, concurrency, termination, drain, redaction."""
import base64
import contextlib
import gc
import hashlib
import io
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
    MAX_SEND_BYTES,
    PROTOCOL_VERSION,
    SEND_CHUNK_BYTES,
    SessionProtocolError,
    receive_message,
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
INPUT_REPORT_CHILD = (
    sys.executable,
    "-u",
    "-c",
    # Raw mode disables echo and canonical buffering, so the child reports only
    # what actually reached the PTY.
    "import sys,tty\n"
    "tty.setraw(0)\n"
    "sys.stdout.write('READY\\n'); sys.stdout.flush()\n"
    "while True:\n"
    "    part=sys.stdin.buffer.read1(65536)\n"
    "    if not part: break\n"
    "    sys.stdout.write('GOT:%d\\n' % len(part)); sys.stdout.flush()\n",
)


def _authed_socket(tmp, timeout=15.0):
    """Connected control socket plus the session token for hand-built frames."""
    token = pathlib.Path(tmp, "session-token").read_text(encoding="utf-8").strip()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(pathlib.Path(tmp, "session-control.sock")))
    return sock, token


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

        The child stays alive while the viewer is idle, so the assertion
        measures backlog delivery only. Racing it against host teardown made
        the test depend on draining within ``FINAL_FLUSH_SECONDS`` of session
        end, which a loaded runner cannot guarantee: teardown is deliberately
        bounded so a wedged viewer cannot keep the host alive.
        """
        from terminal_monitor.session_protocol import FramedReader

        child = (
            sys.executable,
            "-u",
            "-c",
            "import sys,time; print('READY', flush=True); sys.stdin.readline(); "
            "[print(f'row{i}', flush=True) for i in range(4000)]; "
            "print('TAIL_MARKER', flush=True); time.sleep(60)",
        )
        tmp = self._state_dir()
        client = ManagedSessionClient.start(state_dir=tmp, command=child, cwd=tmp)
        self.addCleanup(self._cleanup_client, client, tmp)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        token = pathlib.Path(tmp, "session-token").read_text(encoding="utf-8").strip()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(30.0)
        self.addCleanup(sock.close)
        sock.connect(str(pathlib.Path(tmp, "session-control.sock")))
        send_message(sock, {"version": PROTOCOL_VERSION, "token": token, "op": "stream"})
        # Start the burst only once the viewer is attached, then read nothing
        # for long enough that the per-viewer item queue overflows.
        client.send_bytes(b"go\n")
        time.sleep(1.5)
        reader = FramedReader(sock)
        collected = bytearray()
        self.assertEqual(reader.read_message().get("event"), "snapshot_start")
        while True:
            event = reader.read_message()
            kind = event.get("event")
            if kind == "snapshot_chunk":
                collected += base64.b64decode(event["data_b64"].encode("ascii"))
                continue
            self.assertEqual(kind, "snapshot_end")
            break
        deadline = time.monotonic() + 30.0
        while b"TAIL_MARKER" not in collected and time.monotonic() < deadline:
            event = reader.read_message()
            if event.get("event") != "output":
                break
            collected += base64.b64decode(event["data_b64"].encode("ascii"))
        self.assertIn(b"TAIL_MARKER", collected, "coalesced backlog must keep the tail")
        # The same viewer still receives a deterministic exit event; it is
        # caught up now, so delivery does not depend on teardown timing.
        client.terminate_session()
        exit_deadline = time.monotonic() + 30.0
        exit_event = None
        while exit_event is None and time.monotonic() < exit_deadline:
            event = reader.read_message()
            if event.get("event") == "exit":
                exit_event = event
        self.assertIsNotNone(exit_event, "a stream viewer must observe the exit event")

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


class LargeInputTests(ManagedTestBase):
    """PTY input must survive Base64 expansion up to the documented bound.

    A single 64 KiB frame cannot carry 64 KiB of raw bytes (Base64 adds 4/3),
    so anything past one chunk streams as send_start/send_chunk*/send_end.
    """

    @staticmethod
    def _payload(total):
        # Full byte range, deterministic: proves the transfer is byte-exact and
        # not just newline-safe text.
        return bytes((index * 37 + 11) % 256 for index in range(total))

    @staticmethod
    def _digest_child(total):
        return (
            sys.executable,
            "-u",
            "-c",
            # Raw mode: no echo, no canonical line limit, no byte translation.
            "import sys,tty,hashlib,time\n"
            "tty.setraw(0)\n"
            "sys.stdout.write('READY\\n'); sys.stdout.flush()\n"
            f"expected={total}\n"
            "buf=b''\n"
            "while len(buf)<expected:\n"
            "    part=sys.stdin.buffer.read1(expected-len(buf))\n"
            "    if not part: break\n"
            "    buf+=part\n"
            "sys.stdout.write('DIGEST:'+hashlib.sha256(buf).hexdigest()+':'+str(len(buf))+'\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(30)\n",
        )

    def _expect_digest(self, client, payload):
        want = f"DIGEST:{hashlib.sha256(payload).hexdigest()}:{len(payload)}".encode()
        got = _wait_for(lambda: client.snapshot() if want in client.snapshot() else None, timeout=20.0)
        self.assertIsNotNone(got, f"child never reported {want!r}")

    def test_boundary_sizes_arrive_byte_for_byte(self):
        for total in (1, 1024, 32 * 1024, 48 * 1024, 50 * 1024, 60 * 1024, 64 * 1024):
            with self.subTest(total=total):
                tmp = self._state_dir()
                payload = self._payload(total)
                client = self._start(tmp, command=self._digest_child(total))
                self.assertTrue(_wait_for(lambda client=client: b"READY" in client.snapshot(), timeout=10.0))
                client.send_bytes(payload)
                self._expect_digest(client, payload)

    def test_oversize_rejected_before_any_pty_write(self):
        tmp = self._state_dir()
        client = self._start(tmp, command=INPUT_REPORT_CHILD)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        with self.assertRaises(ValueError) as caught:
            client.send_bytes(self._payload(MAX_SEND_BYTES + 1))
        self.assertIn("E_SESSION_MESSAGE_TOO_LARGE", str(caught.exception))
        time.sleep(0.3)
        self.assertNotIn(b"GOT:", client.snapshot(), "oversize input must not reach the PTY")
        # Session stays usable after the rejection.
        client.send_bytes(b"ok\n")
        self.assertTrue(_wait_for(lambda: b"GOT:" in client.snapshot(), timeout=10.0))

    def test_host_enforces_bound_independently_of_client(self):
        tmp = self._state_dir()
        client = self._start(tmp, command=INPUT_REPORT_CHILD)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        sock, token = _authed_socket(tmp)
        try:
            send_message(
                sock,
                {
                    "version": PROTOCOL_VERSION,
                    "token": token,
                    "op": "send_start",
                    "transfer_id": "t-oversize",
                    "total_bytes": MAX_SEND_BYTES + 1,
                },
            )
            resp = receive_message(sock)
        finally:
            sock.close()
        self.assertFalse(resp.get("ok"))
        self.assertIn("E_SESSION_MESSAGE_TOO_LARGE", str(resp.get("error")))
        time.sleep(0.3)
        self.assertNotIn(b"GOT:", client.snapshot())
        self.assertTrue(client.status().alive)

    def test_backend_send_carries_large_text(self):
        from terminal_monitor.managed_pty import ManagedPTYBackend

        text = "x" * (60 * 1024)
        expected = (text + "\n").encode("utf-8")
        tmp = self._state_dir()
        backend = ManagedPTYBackend(state_dir=tmp)
        backend.start_managed(self._digest_child(len(expected)), cwd=tmp, state_dir=tmp)
        client = backend._client
        self.addCleanup(self._cleanup_client, client, tmp)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        ok, detail = backend.send("ignored", None, text)
        self.assertTrue(ok, detail)
        self._expect_digest(client, expected)


class ChunkedInputTransferTests(ManagedTestBase):
    """Malformed chunked transfers must fail closed with zero PTY bytes."""

    def _session(self):
        tmp = self._state_dir()
        client = self._start(tmp, command=INPUT_REPORT_CHILD)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        return tmp, client

    @staticmethod
    def _start_frame(token, transfer_id, total):
        return {
            "version": PROTOCOL_VERSION,
            "token": token,
            "op": "send_start",
            "transfer_id": transfer_id,
            "total_bytes": total,
        }

    @staticmethod
    def _chunk_frame(transfer_id, seq, data):
        return {
            "op": "send_chunk",
            "transfer_id": transfer_id,
            "seq": seq,
            "data_b64": base64.b64encode(data).decode("ascii"),
        }

    def test_valid_multi_frame_transfer_is_accepted(self):
        tmp, client = self._session()
        payload = bytes((index * 11 + 3) % 256 for index in range(SEND_CHUNK_BYTES + 777))
        sock, token = _authed_socket(tmp)
        try:
            send_message(sock, self._start_frame(token, "t-ok", len(payload)))
            for seq, offset in enumerate(range(0, len(payload), SEND_CHUNK_BYTES)):
                send_message(sock, self._chunk_frame("t-ok", seq, payload[offset : offset + SEND_CHUNK_BYTES]))
            send_message(sock, {"op": "send_end", "transfer_id": "t-ok", "total_bytes": len(payload)})
            resp = receive_message(sock)
        finally:
            sock.close()
        self.assertTrue(resp.get("ok"), resp)
        self.assertTrue(_wait_for(lambda: b"GOT:" in client.snapshot(), timeout=10.0))

    def test_malformed_transfers_write_nothing(self):
        tmp, client = self._session()
        cases = {
            "out_of_order": lambda token: [
                self._start_frame(token, "t1", 8),
                self._chunk_frame("t1", 1, b"abcd"),
            ],
            "duplicate_seq": lambda token: [
                self._start_frame(token, "t2", 8),
                self._chunk_frame("t2", 0, b"abcd"),
                self._chunk_frame("t2", 0, b"efgh"),
            ],
            "missing_chunk": lambda token: [
                self._start_frame(token, "t3", 8),
                self._chunk_frame("t3", 0, b"abcd"),
                {"op": "send_end", "transfer_id": "t3", "total_bytes": 8},
            ],
            "malformed_base64": lambda token: [
                self._start_frame(token, "t4", 8),
                {"op": "send_chunk", "transfer_id": "t4", "seq": 0, "data_b64": "!!!!"},
            ],
            "wrong_total_at_end": lambda token: [
                self._start_frame(token, "t5", 4),
                self._chunk_frame("t5", 0, b"abcd"),
                {"op": "send_end", "transfer_id": "t5", "total_bytes": 9},
            ],
            "chunk_exceeds_total": lambda token: [
                self._start_frame(token, "t6", 4),
                self._chunk_frame("t6", 0, b"abcdefghij"),
            ],
            "foreign_transfer_id": lambda token: [
                self._start_frame(token, "t7", 4),
                self._chunk_frame("other", 0, b"abcd"),
            ],
            "unknown_op_mid_transfer": lambda token: [
                self._start_frame(token, "t8", 4),
                {"op": "resize", "cols": 10, "rows": 10},
            ],
        }
        for name, build in cases.items():
            with self.subTest(case=name):
                sock, token = _authed_socket(tmp)
                try:
                    for frame in build(token):
                        send_message(sock, frame)
                    try:
                        resp = receive_message(sock)
                    except SessionProtocolError:
                        resp = {"ok": False, "error": "closed"}
                finally:
                    sock.close()
                self.assertFalse(resp.get("ok"), f"{name} should be rejected")
                time.sleep(0.2)
                self.assertNotIn(b"GOT:", client.snapshot(), f"{name} must not reach the PTY")
                self.assertTrue(client.status().alive, f"host must survive {name}")

    def test_eof_before_send_end_writes_nothing(self):
        tmp, client = self._session()
        sock, token = _authed_socket(tmp)
        try:
            send_message(sock, self._start_frame(token, "t-eof", 2 * SEND_CHUNK_BYTES))
            send_message(sock, self._chunk_frame("t-eof", 0, b"z" * SEND_CHUNK_BYTES))
        finally:
            sock.close()
        time.sleep(0.4)
        self.assertNotIn(b"GOT:", client.snapshot(), "an unfinished transfer must not reach the PTY")
        self.assertTrue(client.status().alive)
        # Still usable afterwards.
        client.send_bytes(b"after\n")
        self.assertTrue(_wait_for(lambda: b"GOT:" in client.snapshot(), timeout=10.0))


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
            self.assertTrue(saved["active"])
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

    def test_create_share_refuses_replacement_when_previous_stop_fails(self):
        """Fail closed: a second remote link must never be created."""
        from terminal_monitor.cli import _create_share

        with tempfile.TemporaryDirectory() as tmp:
            original = {"provider": "shell.online", "active": True, "read_only": True, "encrypted": True, "session_id": "old", "share_url": "https://old"}
            pathlib.Path(tmp, "remote-share.json").write_text(json.dumps(original), encoding="utf-8")
            provider = _StopFailsProvider()
            with self.assertRaisesRegex(RuntimeError, "E_REMOTE_SHARE_PREVIOUS_ACTIVE"):
                _create_share(provider, tmp)
            self.assertEqual(provider.stop_calls, ["old"])
            self.assertFalse(provider.share_calls, "share_read_only_with_password must not be called")
            saved = json.loads(pathlib.Path(tmp, "remote-share.json").read_text(encoding="utf-8"))
            self.assertTrue(saved["active"], "previous share must stay marked active")
            self.assertEqual(saved["session_id"], "old")
            self.assertEqual(saved, original, "previous metadata must be preserved verbatim")
            self.assertNotIn("new", saved["share_url"])

    def test_create_share_failure_never_leaks_provider_detail(self):
        from terminal_monitor.cli import _create_share

        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, "remote-share.json").write_text(
                json.dumps({"provider": "shell.online", "active": True, "read_only": True, "session_id": "old"}),
                encoding="utf-8",
            )
            provider = _StopFailsProvider(detail="kill failed: e2ee password=secret-value token=abcd1234 (stderr)")
            with self.assertRaises(RuntimeError) as ctx:
                _create_share(provider, tmp)
            message = str(ctx.exception)
            for leaked in ("secret-value", "password=", "token=", "stderr"):
                self.assertNotIn(leaked, message)
            persisted = pathlib.Path(tmp, "remote-share.json").read_text(encoding="utf-8")
            self.assertNotIn("secret-value", persisted)
            self.assertNotIn("password", persisted)

    @unittest.skipIf(os.name != "posix", "managed PTY requires POSIX")
    def test_run_share_returns_nonzero_when_previous_stop_fails(self):
        import argparse

        from terminal_monitor.cli import _run_share
        from terminal_monitor.shell_online import ShellOnlineProvider

        tmp, client = self._live_session()
        pathlib.Path(tmp, "remote-share.json").write_text(
            json.dumps({"provider": "shell.online", "active": True, "read_only": True, "session_id": "old-share"}),
            encoding="utf-8",
        )

        def _stop_fails(self, session_id):
            return (False, "provider kill failed: password=secret-value")

        def _never_share(self, *, state_dir):
            raise AssertionError("must not be called")

        with mock.patch.object(ShellOnlineProvider, "stop", _stop_fails), mock.patch.object(ShellOnlineProvider, "share_read_only_with_password", _never_share):
            args = argparse.Namespace(state_dir=tmp, project_dir=tmp, provider="shell-online")
            config = argparse.Namespace(state_dir=tmp, project_dir=tmp)
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = _run_share(args, config)
        self.assertNotEqual(code, 0)
        self.assertIn("E_REMOTE_SHARE_PREVIOUS_ACTIVE", stderr.getvalue())
        self.assertNotIn("secret-value", stderr.getvalue() + stdout.getvalue())
        self.assertNotIn("URL:", stdout.getvalue())
        self.assertNotIn("Browser password", stdout.getvalue())
        saved = json.loads(pathlib.Path(tmp, "remote-share.json").read_text(encoding="utf-8"))
        self.assertTrue(saved["active"])
        self.assertEqual(saved["session_id"], "old-share")
        self.assertTrue(client.status().alive, "share failure must not touch the managed agent")

    @unittest.skipIf(os.name != "posix", "managed PTY requires POSIX")
    def test_automatic_remote_provider_failure_is_non_fatal(self):
        """`--remote-provider shell-online` aborts sharing but keeps supervising."""
        import shlex

        from terminal_monitor import cli as cli_module
        from terminal_monitor.monitor import TerminalMonitor
        from terminal_monitor.shell_online import ShellOnlineProvider

        tmp = self._state_dir()
        pathlib.Path(tmp, "remote-share.json").write_text(
            json.dumps({"provider": "shell.online", "active": True, "read_only": True, "session_id": "old-share"}),
            encoding="utf-8",
        )
        run_calls: list = []

        def _fake_run(self):
            run_calls.append(self.state_dir)
            return 0

        def _stop_fails(self, session_id):
            return (False, "provider kill failed: password=secret-value")

        def _never_share(self, *, state_dir):
            raise AssertionError("must not be called")

        argv = [
            "terminal-monitor",
            "supervise",
            "--backend",
            "pty",
            "--state-dir",
            tmp,
            "--project-dir",
            tmp,
            "--agent-command",
            shlex.join(LONG_CHILD),
            "--remote-provider",
            "shell-online",
            "--no-web-ui",
        ]
        self.addCleanup(self._cleanup_state_dir_session, tmp)
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(TerminalMonitor, "run", _fake_run),
            mock.patch.object(ShellOnlineProvider, "stop", _stop_fails),
            mock.patch.object(ShellOnlineProvider, "share_read_only_with_password", _never_share),
        ):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli_module.main()
        errors = stderr.getvalue()
        self.assertIn("REMOTE_PROVIDER_ERROR", errors)
        self.assertIn("E_REMOTE_SHARE_PREVIOUS_ACTIVE", errors)
        self.assertNotIn("secret-value", errors + stdout.getvalue())
        # Local supervision continued despite the remote failure.
        self.assertEqual(code, 0)
        self.assertEqual(run_calls, [tmp])
        saved = json.loads(pathlib.Path(tmp, "remote-share.json").read_text(encoding="utf-8"))
        self.assertTrue(saved["active"])
        self.assertEqual(saved["session_id"], "old-share")
        # The managed agent started and survived the remote-provider failure.
        client = ManagedSessionClient.connect(tmp)
        self.addCleanup(self._cleanup_client, client, tmp)
        self.assertTrue(client.status().alive)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))

    @unittest.skipIf(os.name != "posix", "managed PTY requires POSIX")
    def test_unshare_keeps_agent_alive(self):
        from terminal_monitor.cli import _run_unshare
        from terminal_monitor.shell_online import ShellOnlineProvider

        tmp, client = self._live_session()
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
            with contextlib.redirect_stdout(io.StringIO()):
                code = _run_unshare(args, config)
        self.assertEqual(code, 0)
        saved = json.loads(pathlib.Path(tmp, "remote-share.json").read_text(encoding="utf-8"))
        self.assertFalse(saved.get("active"))
        self.assertTrue(client.status().alive)

    @unittest.skipIf(os.name != "posix", "managed PTY requires POSIX")
    def test_unshare_failure_preserves_active_metadata(self):
        """A failed provider stop must never be recorded as a stopped share."""
        import argparse

        from terminal_monitor.cli import _run_unshare
        from terminal_monitor.shell_online import ShellOnlineProvider

        tmp, client = self._live_session()
        pathlib.Path(tmp, "remote-share.json").write_text(
            json.dumps({"provider": "shell.online", "active": True, "read_only": True, "session_id": "old-share"}),
            encoding="utf-8",
        )

        def _stop_fails(self, session_id):
            return (False, "provider kill failed: password=secret-value")

        with mock.patch.object(ShellOnlineProvider, "stop", _stop_fails):
            args = argparse.Namespace(state_dir=tmp, project_dir=tmp, session_id=None)
            config = argparse.Namespace(state_dir=tmp, project_dir=tmp)
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = _run_unshare(args, config)
        self.assertNotEqual(code, 0)
        self.assertNotIn("stopped", stdout.getvalue())
        self.assertNotIn("secret-value", stdout.getvalue() + stderr.getvalue())
        saved = json.loads(pathlib.Path(tmp, "remote-share.json").read_text(encoding="utf-8"))
        self.assertTrue(saved["active"], "a failed stop must keep the share marked active")
        self.assertEqual(saved["session_id"], "old-share")
        self.assertTrue(client.status().alive, "unshare failure must not stop the agent")

    @unittest.skipIf(os.name != "posix", "managed PTY requires POSIX")
    def test_unshare_failure_without_saved_metadata_invents_nothing(self):
        import argparse

        from terminal_monitor.cli import _run_unshare
        from terminal_monitor.shell_online import ShellOnlineProvider

        def _stop_fails(self, session_id):
            return (False, "provider kill failed")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ShellOnlineProvider, "stop", _stop_fails):
                args = argparse.Namespace(state_dir=tmp, project_dir=tmp, session_id="explicit-id")
                config = argparse.Namespace(state_dir=tmp, project_dir=tmp)
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = _run_unshare(args, config)
            self.assertNotEqual(code, 0)
            self.assertFalse(pathlib.Path(tmp, "remote-share.json").exists(), "no inactive record may be invented")

    def _live_session(self):
        tmp = self._state_dir()
        client = ManagedSessionClient.start(state_dir=tmp, command=LONG_CHILD, cwd=tmp)
        self.addCleanup(self._cleanup_client, client, tmp)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        return tmp, client

    def _cleanup_state_dir_session(self, tmp):
        with contextlib.suppress(OSError, ValueError, RuntimeError, SessionProtocolError, ConnectionError, PermissionError):
            client = ManagedSessionClient.connect(tmp)
            with contextlib.suppress(OSError, ValueError, RuntimeError, SessionProtocolError, ConnectionError, PermissionError):
                client.terminate_session()
            with contextlib.suppress(OSError, ValueError, RuntimeError):
                client.close()

    def _cleanup_client(self, client, tmp):
        with contextlib.suppress(OSError, ValueError, RuntimeError, SessionProtocolError, ConnectionError, PermissionError):
            client.terminate_session()
        with contextlib.suppress(OSError, ValueError, RuntimeError):
            client.close()


class _StopFailsProvider:
    """Provider whose stop always fails; sharing is forbidden."""

    def __init__(self, detail="provider kill failed"):
        self._detail = detail
        self.stop_calls: list = []
        self.share_calls: list = []

    def stop(self, session_id):
        self.stop_calls.append(session_id)
        return (False, self._detail)

    def share_read_only_with_password(self, *, state_dir):
        self.share_calls.append(state_dir)
        raise AssertionError("must not be called")


if __name__ == "__main__":
    unittest.main()

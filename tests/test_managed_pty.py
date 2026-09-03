import contextlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest

REPO_ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT))


import terminal_monitor  # noqa: E402
from terminal_monitor.managed_pty import ManagedPTYBackend, ManagedSessionClient  # noqa: E402
from terminal_monitor.session_protocol import SessionProtocolError  # noqa: E402

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
class ManagedPTYTests(unittest.TestCase):
    def _state_dir(self):
        """State dir that outlives client cleanup (cleanups run LIFO).

        A ``with TemporaryDirectory()`` block would delete the control socket
        before teardown runs, so the session could no longer be terminated and
        the detached host would survive the test run.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return tmp

    def _start_client(self, tmp, command=CHILD_ECHO):
        client = ManagedSessionClient.start(state_dir=tmp, command=command, cwd=tmp)
        self.addCleanup(self._cleanup, client, tmp)
        return client

    def _cleanup(self, client, tmp=None):
        with contextlib.suppress(OSError, ValueError, RuntimeError, SessionProtocolError, ConnectionError, PermissionError):
            client.terminate_session()
        with contextlib.suppress(OSError, ValueError, RuntimeError):
            client.close()
        if tmp is None:
            return
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

    def test_start_and_connect_and_reconnect_sees_replay(self):
        tmp = self._state_dir()
        client = self._start_client(tmp)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        status1 = client.status()
        client.close()
        # Closing the client leaves host/agent alive.
        client2 = ManagedSessionClient.connect(tmp)
        self.addCleanup(self._cleanup, client2, tmp)
        status2 = client2.status()
        self.assertEqual(status1.session_id, status2.session_id)
        self.assertIn(b"READY", client2.snapshot())

    def _cleanup_backend(self, backend, tmp):
        # Terminate through the live client first: a closed client can no longer
        # reach the host, which would leave the detached session running.
        client = backend._client
        if client is not None:
            self._cleanup(client, tmp)
        backend.close()

    def test_backend_get_tab_returns_replay_text(self):
        tmp = self._state_dir()
        backend = ManagedPTYBackend(state_dir=tmp)
        backend.start_managed(CHILD_ECHO, cwd=tmp, state_dir=tmp)
        self.addCleanup(self._cleanup_backend, backend, tmp)

        def _ready_tab():
            # Assert on the snapshot that actually carries READY: an earlier tab
            # may legitimately predate the child's first write.
            tab = backend.get_tab("anything")
            return tab if tab.get("ok") and "READY" in tab.get("hist", "") else None

        tab = _wait_for(_ready_tab, timeout=10.0)
        self.assertIsNotNone(tab, "expected a tab snapshot containing READY")
        self.assertIn("READY", tab.get("hist", ""))
        self.assertTrue(tab.get("busy"))

    def test_backend_send_appends_enter_once(self):
        tmp = self._state_dir()
        backend = ManagedPTYBackend(state_dir=tmp)
        backend.start_managed(CHILD_ECHO, cwd=tmp, state_dir=tmp)
        self.addCleanup(self._cleanup_backend, backend, tmp)
        self.assertTrue(_wait_for(lambda: b"READY" in backend._client.snapshot(), timeout=10.0))
        ok, detail = backend.send("ignored", None, "hello")
        self.assertTrue(ok, detail)
        self.assertTrue(_wait_for(lambda: b"ECHO:hello" in backend._client.snapshot(), timeout=10.0))

    def test_backend_send_key_maps_vocabulary(self):
        tmp = self._state_dir()
        backend = ManagedPTYBackend(state_dir=tmp)
        backend.start_managed(CHILD_ECHO, cwd=tmp, state_dir=tmp)
        self.addCleanup(self._cleanup_backend, backend, tmp)
        ok, _ = backend.send_key("ignored", None, "enter")
        self.assertTrue(ok)
        ok, _ = backend.send_key("ignored", None, "ctrl+c")
        self.assertTrue(ok)
        ok, _ = backend.send_key("ignored", None, "a")
        self.assertTrue(ok)

    def test_backend_get_pids_and_owns_process(self):
        tmp = self._state_dir()
        backend = ManagedPTYBackend(state_dir=tmp)
        self.assertTrue(backend.owns_process)
        self.assertEqual(backend.name(), "pty")
        backend.start_managed(CHILD_ECHO, cwd=tmp, state_dir=tmp)
        self.addCleanup(self._cleanup_backend, backend, tmp)
        pids = _wait_for(lambda: backend.get_pids("ignored") or None, timeout=10.0)
        self.assertTrue(pids)
        self.assertGreater(pids[0], 0)

    def test_get_backend_pty(self):
        backend = terminal_monitor.get_backend("pty")
        self.assertEqual(backend.name(), "pty")
        self.assertTrue(backend.owns_process)

    def test_stream_yields_snapshot_then_live(self):
        tmp = self._state_dir()
        client = self._start_client(tmp)
        self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
        chunks = []
        for chunk in client.stream():
            chunks.append(chunk)
            if b"READY" in b"".join(chunks):
                break
            if len(chunks) > 5:
                break
        self.assertTrue(b"READY" in b"".join(chunks))


if __name__ == "__main__":
    unittest.main()

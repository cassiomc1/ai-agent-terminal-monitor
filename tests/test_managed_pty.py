import contextlib
import os
import pathlib
import sys
import tempfile
import time
import unittest

REPO_ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT))


import terminal_monitor  # noqa: E402
from terminal_monitor.managed_pty import ManagedPTYBackend, ManagedSessionClient  # noqa: E402

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
        except (OSError, ValueError, RuntimeError, ConnectionError, PermissionError):
            pass
        time.sleep(interval)
    return None


@unittest.skipIf(os.name != "posix", "managed PTY requires POSIX")
class ManagedPTYTests(unittest.TestCase):
    def _start_client(self, tmp, command=CHILD_ECHO):
        client = ManagedSessionClient.start(state_dir=tmp, command=command, cwd=tmp)
        self.addCleanup(self._cleanup, client)
        return client

    def _cleanup(self, client):
        with contextlib.suppress(OSError, ValueError, RuntimeError, ConnectionError, PermissionError):
            client.terminate_session()
        with contextlib.suppress(OSError, ValueError):
            client.close()

    def test_start_and_connect_and_reconnect_sees_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = self._start_client(tmp)
            self.assertTrue(_wait_for(lambda: b"READY" in client.snapshot(), timeout=10.0))
            status1 = client.status()
            client.close()
            # Closing the client leaves host/agent alive.
            client2 = ManagedSessionClient.connect(tmp)
            self.addCleanup(self._cleanup, client2)
            status2 = client2.status()
            self.assertEqual(status1.session_id, status2.session_id)
            self.assertIn(b"READY", client2.snapshot())

    def test_backend_get_tab_returns_replay_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = ManagedPTYBackend(state_dir=tmp)
            backend.start_managed(CHILD_ECHO, cwd=tmp, state_dir=tmp)
            self.addCleanup(backend.close)
            try:
                tab = _wait_for(lambda: backend.get_tab("anything").get("ok") and b"READY" in backend.get_tab("x").get("hist", "").encode() and backend.get_tab("x"), timeout=10.0)
            finally:
                pass
            self.assertTrue(tab.get("ok"))
            self.assertIn("READY", tab.get("hist", ""))
            self.assertTrue(tab.get("busy"))

    def test_backend_send_appends_enter_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = ManagedPTYBackend(state_dir=tmp)
            backend.start_managed(CHILD_ECHO, cwd=tmp, state_dir=tmp)
            self.addCleanup(backend.close)
            self.addCleanup(lambda: self._cleanup(backend._client) if backend._client else None)
            self.assertTrue(_wait_for(lambda: b"READY" in backend._client.snapshot(), timeout=10.0))
            ok, detail = backend.send("ignored", None, "hello")
            self.assertTrue(ok, detail)
            self.assertTrue(_wait_for(lambda: b"ECHO:hello" in backend._client.snapshot(), timeout=10.0))

    def test_backend_send_key_maps_vocabulary(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = ManagedPTYBackend(state_dir=tmp)
            backend.start_managed(CHILD_ECHO, cwd=tmp, state_dir=tmp)
            self.addCleanup(backend.close)
            self.addCleanup(lambda: self._cleanup(backend._client) if backend._client else None)
            ok, _ = backend.send_key("ignored", None, "enter")
            self.assertTrue(ok)
            ok, _ = backend.send_key("ignored", None, "ctrl+c")
            self.assertTrue(ok)
            ok, _ = backend.send_key("ignored", None, "a")
            self.assertTrue(ok)

    def test_backend_get_pids_and_owns_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = ManagedPTYBackend(state_dir=tmp)
            self.assertTrue(backend.owns_process)
            self.assertEqual(backend.name(), "pty")
            backend.start_managed(CHILD_ECHO, cwd=tmp, state_dir=tmp)
            self.addCleanup(backend.close)
            self.addCleanup(lambda: self._cleanup(backend._client) if backend._client else None)
            pids = _wait_for(lambda: backend.get_pids("ignored") or None, timeout=10.0)
            self.assertTrue(pids)
            self.assertGreater(pids[0], 0)

    def test_get_backend_pty(self):
        backend = terminal_monitor.get_backend("pty")
        self.assertEqual(backend.name(), "pty")
        self.assertTrue(backend.owns_process)

    def test_stream_yields_snapshot_then_live(self):
        with tempfile.TemporaryDirectory() as tmp:
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

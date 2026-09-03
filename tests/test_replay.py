import pathlib
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT))

from terminal_monitor.replay import ReplayBuffer  # noqa: E402


class ReplayBufferTests(unittest.TestCase):
    def test_append_smaller_than_capacity(self):
        replay = ReplayBuffer(capacity_bytes=16)
        replay.append(b"hello")
        self.assertEqual(replay.snapshot(), b"hello")
        self.assertEqual(replay.size_bytes, 5)

    def test_append_exactly_capacity(self):
        replay = ReplayBuffer(capacity_bytes=5)
        replay.append(b"abcde")
        self.assertEqual(replay.snapshot(), b"abcde")
        self.assertEqual(replay.size_bytes, 5)

    def test_overflow_keeps_newest_bytes(self):
        replay = ReplayBuffer(capacity_bytes=5)
        replay.append(b"abc")
        replay.append(b"def")
        self.assertEqual(replay.snapshot(), b"bcdef")
        self.assertEqual(replay.size_bytes, 5)

    def test_large_chunk_is_trimmed_to_capacity(self):
        replay = ReplayBuffer(capacity_bytes=4)
        replay.append(b"abcdef")
        self.assertEqual(replay.snapshot(), b"cdef")

    def test_limit_returns_newest_suffix(self):
        replay = ReplayBuffer(capacity_bytes=16)
        replay.append(b"hello world")
        self.assertEqual(replay.snapshot(limit_bytes=5), b"world")
        self.assertEqual(replay.snapshot(limit_bytes=100), b"hello world")
        self.assertEqual(replay.snapshot(limit_bytes=0), b"")

    def test_invalid_capacity_raises(self):
        with self.assertRaises(ValueError):
            ReplayBuffer(capacity_bytes=0)
        with self.assertRaises(ValueError):
            ReplayBuffer(capacity_bytes=-10)

    def test_empty_append_is_harmless(self):
        replay = ReplayBuffer(capacity_bytes=8)
        replay.append(b"")
        replay.append(b"hi")
        replay.append(b"")
        self.assertEqual(replay.snapshot(), b"hi")
        self.assertEqual(replay.size_bytes, 2)


if __name__ == "__main__":
    unittest.main()

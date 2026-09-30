"""Platform branches plus real cross-process locking on the test host."""
import errno
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kbar_download import storage
from kbar_download.model import DataError


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kbar 測試 ")
        self.addCleanup(self.tmp.cleanup)
        self.store = storage.Store(self.tmp.name)

    def test_windows_lock_offsets_and_release_after_exception(self):
        calls = []
        def lock(fd, mode, count):
            calls.append((os.lseek(fd, 0, os.SEEK_CUR), mode, count))
        module = SimpleNamespace(locking=lock, LK_NBLCK=2, LK_UNLCK=0)
        with patch.object(storage, "WINDOWS", True), patch.dict(sys.modules, msvcrt=module):
            with self.assertRaisesRegex(ValueError, "body"):
                with self.store.lock():
                    raise ValueError("body")
        self.assertEqual(calls, [(0, 2, 1), (0, 0, 1)])
        self.assertTrue((self.store.root / ".lock").exists())

    def test_windows_conflict_does_not_unlock_unowned_region(self):
        fn = Mock(side_effect=OSError(errno.EACCES, "locked"))
        module = SimpleNamespace(locking=fn, LK_NBLCK=2, LK_UNLCK=0)
        with patch.object(storage, "WINDOWS", True), patch.dict(sys.modules, msvcrt=module):
            with self.assertRaises(DataError):
                with self.store.lock():
                    self.fail("lock must fail")
        self.assertEqual(fn.call_count, 1)

    def test_windows_io_error_is_not_lock_conflict(self):
        module = SimpleNamespace(locking=Mock(side_effect=OSError(errno.EIO, "io")), LK_NBLCK=2, LK_UNLCK=0)
        with patch.object(storage, "WINDOWS", True), patch.dict(sys.modules, msvcrt=module):
            with self.assertRaises(OSError):
                with self.store.lock():
                    pass

    def test_windows_file_sync_remains_directory_sync_skipped(self):
        real = os.fsync
        with patch.object(storage, "WINDOWS", True), patch.object(storage.os, "fsync", wraps=real) as sync:
            path = self.store.root / "中文 檔案.json"
            storage.atomic_write(path, b'{}')
            self.assertEqual(path.read_bytes(), b'{}')
            self.assertEqual(sync.call_count, 1)

    def test_replace_denied_preserves_journal_then_recovers(self):
        state = {"format_version": 1, "files": {}}
        real = os.replace
        with patch.object(storage.os, "replace", wraps=real) as replace:
            def fail_csv(src, dst):
                if str(dst).endswith(".csv"):
                    raise PermissionError("occupied")
                return real(src, dst)
            replace.side_effect = fail_csv
            with self.assertRaises(PermissionError):
                self.store.commit(state, {"中文/2026-01.csv": b'ts\n1\n'})
        self.assertTrue(self.store.journal.exists())
        self.store.recover()
        self.assertEqual((self.store.root / "中文/2026-01.csv").read_bytes(), b'ts\n1\n')
        self.assertFalse(self.store.journal.exists())

    def test_real_process_lock_release_after_termination(self):
        script = """
import sys
from kbar_download.storage import Store
with Store(sys.argv[1]).lock():
    print('locked', flush=True)
    sys.stdin.read()
"""
        child = subprocess.Popen([sys.executable, "-c", script, self.tmp.name],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            with self.assertRaises(DataError):
                with self.store.lock():
                    pass
            child.terminate()
            child.wait(timeout=10)
            with self.store.lock():
                pass
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)

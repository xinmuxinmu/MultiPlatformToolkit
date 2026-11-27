import pathlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from modules.ai_checkin.vendor import doubao_checkin as doubao


class DoubaoPythonCopyTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Win32 API test")
    def test_native_shared_handle_copies_fixture_and_closes_handle(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = pathlib.Path(temporary) / "source.fixture"
            target = pathlib.Path(temporary) / "copy.fixture"
            fixture = b"isolated fake database" * 100000
            source.write_bytes(fixture)
            copied, note = doubao._windows_shared_copy(source, target)
            self.assertTrue(copied, note)
            self.assertEqual(target.read_bytes(), fixture)
            source.unlink()

    def test_sqlite_snapshot_includes_committed_wal_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = pathlib.Path(temporary) / "source.sqlite"
            target = pathlib.Path(temporary) / "snapshot.sqlite"
            connection = sqlite3.connect(str(source))
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA wal_autocheckpoint=0")
                connection.execute("CREATE TABLE messages(value TEXT)")
                connection.execute("INSERT INTO messages VALUES ('fixture')")
                connection.commit()
                self.assertTrue(pathlib.Path(str(source) + "-wal").is_file())
                self.assertTrue(doubao._sqlite_cookie_snapshot(source, target))
                with closing(sqlite3.connect(str(target))) as snapshot:
                    self.assertEqual(snapshot.execute("SELECT value FROM messages").fetchall(), [("fixture",)])
                target.unlink()  # SQLite handle must be closed, including on Windows.
            finally:
                connection.close()

    def test_exclusive_lock_is_bounded_and_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = pathlib.Path(temporary) / "locked.sqlite"
            target = pathlib.Path(temporary) / "snapshot.sqlite"
            connection = sqlite3.connect(str(source))
            try:
                connection.execute("CREATE TABLE messages(value TEXT)")
                connection.commit()
                connection.execute("BEGIN EXCLUSIVE")
                self.assertFalse(doubao._sqlite_cookie_snapshot(source, target, timeout=.1))
                self.assertFalse(target.exists())
            finally:
                connection.close()

    def test_unreadable_database_returns_clear_limitation_without_shell(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = pathlib.Path(temporary) / "locked.sqlite"
            target = pathlib.Path(temporary) / "snapshot.sqlite"
            with patch.object(doubao, "_sqlite_cookie_snapshot", return_value=False), \
                 patch("shutil.copy", side_effect=PermissionError("sensitive source path")), \
                 patch.object(doubao, "_windows_shared_copy", return_value=(False, "独占锁")), \
                 patch.object(doubao, "_windows_esentutl_copy", return_value=False), \
                 patch.object(doubao.sys, "platform", "win32"), \
                 patch.object(doubao.subprocess, "run") as process:
                copied, note = doubao._copy_cookie_database(source, target)
            self.assertFalse(copied)
            self.assertIn("退出豆包后重试", note)
            self.assertIn("加密缓存", note)
            self.assertNotIn("sensitive source path", note)
            process.assert_not_called()

    def test_shared_copy_fallback_is_used_without_admin_rerun(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = pathlib.Path(temporary) / "cookies.sqlite"
            target = pathlib.Path(temporary) / "snapshot.sqlite"
            def shared_copy(src, dst):
                dst.write_bytes(b"fixture")
                return True, "shared read"
            with patch.object(doubao, "_sqlite_cookie_snapshot", return_value=False), \
                 patch("shutil.copy", side_effect=PermissionError()), \
                 patch.object(doubao, "_windows_shared_copy", side_effect=shared_copy) as shared, \
                 patch.object(doubao, "_windows_esentutl_copy") as esentutl, \
                 patch.object(doubao.sys, "platform", "win32"):
                copied, note = doubao._copy_cookie_database(source, target)
            self.assertTrue(copied)
            self.assertEqual(target.read_bytes(), b"fixture")
            shared.assert_called_once_with(source, target)
            esentutl.assert_not_called()

    def test_failure_falls_back_to_encrypted_cache(self):
        cached = [{"name": "fixture", "value": "fixture", "domain": "example.test"}]
        with patch.object(doubao, "_read_client_cookies_live", return_value=([], "独占锁")), \
             patch.object(doubao, "_load_cookie_cache", return_value=(cached, "本机加密缓存")), \
             patch.object(doubao, "_save_cookie_cache") as save:
            result, reason = doubao.read_client_cookies()
        self.assertEqual(result, cached)
        self.assertEqual(reason, "本机加密缓存")
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()

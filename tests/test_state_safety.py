"""Targeted regressions for crash durability, invalid state and clock rollback."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from submission_state import InvalidArgument, LimitError, StateStore


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.state = StateStore(self.path)

    def test_historical_clock_guard_cannot_unlock_cooldown(self):
        attempt = self.state.reserve("identity", now=1000)
        self.state.finish(attempt, "success", now=1000)
        self.state.recover_pending(now=2000)  # no-op writes a future guard
        self.assertTrue(self.state.status("identity", now=1100)["blocked"])
        self.assertEqual(self.state.status("identity", now=1100)["wait_seconds"], 200)
        with self.assertRaises(LimitError):
            self.state.reserve("identity", now=1100)
        self.state.reserve("identity", now=1300)
        # 时钟护栏是全局时钟安全保护，但不能给"未提交过的其他身份"制造冷却
        self.assertEqual(self.state.status("other-identity", now=1100)["wait_seconds"], 0)
        self.state.reserve("other-identity", now=1100)

    def test_history_does_not_create_shared_cooldown(self):
        """远端历史只约束被同步的那个身份；global_state 仅统计。"""
        self.state.sync_history("account-a", 1, 1000)
        st = self.state.status("account-b", now=1100)
        self.assertEqual(st["wait_seconds"], 0)          # 不再共享冷却
        self.assertFalse(st["blocked"])
        self.assertEqual(st["global_last_at"], 1000)     # 仅统计展示
        self.assertGreater(st["global_wait_seconds"], 0)  # 仅展示，不参与判定
        self.state.reserve("account-b", now=1100)        # 切号可立即提交
        with self.assertRaises(LimitError) as ctx:
            self.state.reserve("account-a", now=1100)    # A 自己受远端时间约束
        self.assertEqual(ctx.exception.reason, "cooldown")
        self.assertEqual(ctx.exception.wait_seconds, 200)

    def test_memory_and_empty_database_paths_rejected(self):
        for path in ("", ":memory:"):
            with self.assertRaises(InvalidArgument):
                StateStore(path)

    def test_wal_reservations_use_full_sync(self):
        db = self.state._connect()
        try:
            self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            db.close()

    def test_corrupt_guard_stops_instead_of_resetting(self):
        for value in ("broken", "nan", "inf"):
            db = sqlite3.connect(self.path)
            try:
                db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_seen_now',?)", (value,))
                db.commit()
            finally:
                db.close()
            with self.assertRaises(sqlite3.DatabaseError):
                self.state.status("identity", now=1000)
            with self.assertRaises(sqlite3.DatabaseError):
                self.state.reserve("identity", now=1000)

    def test_unknown_schema_version_stops(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(sqlite3.DatabaseError):
            StateStore(self.path)


if __name__ == "__main__":
    unittest.main()

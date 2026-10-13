"""Targeted regressions for crash durability, invalid state and clock rollback."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from submission_state import (
    MIN_INTERVAL_SECONDS,
    InvalidArgument,
    LimitError,
    StateStore,
)


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

    def test_no_cumulative_quota_21_then_100_history(self):
        """需求：历史 21 次（乃至 100 次）后，只要无冷却仍可继续提交。"""
        for count in (21, 100):
            with self.subTest(count=count):
                state = StateStore(Path(self.temp.name) / f"q{count}.sqlite3")
                state.sync_history("acct", count, None)
                st = state.status("acct", now=10_000)
                self.assertEqual(st["used"], count)
                self.assertIs(st["exhausted"], False)
                self.assertIsNone(st["remaining"])
                self.assertIsNone(st["quota"])
                self.assertFalse(st["blocked"])
                state.reserve("acct", now=10_000)

    def test_reserve_never_exhausts_and_used_keeps_accumulating(self):
        state = self.state
        for expected in range(1, 14):
            now = float(expected) * MIN_INTERVAL_SECONDS
            aid = state.reserve("acct", now=now)
            state.finish(aid, "success", now=now)
            st = state.status("acct", now=now)
            self.assertEqual(st["used"], expected)
            self.assertIs(st["exhausted"], False)
        # 第 14 次之后（早已超过旧常量 10）冷却到期仍可提交
        now = 14 * MIN_INTERVAL_SECONDS
        aid = state.reserve("acct", now=now)
        state.finish(aid, "success", now=now)
        self.assertEqual(state.status("acct", now=now)["used"], 14)

    def test_history_21_with_recent_time_still_cools(self):
        self.state.sync_history("acct", 21, 1000)
        st = self.state.status("acct", now=1100)
        self.assertEqual(st["used"], 21)
        self.assertTrue(st["blocked"])
        self.assertEqual(st["wait_seconds"], 200)
        with self.assertRaises(LimitError) as ctx:
            self.state.reserve("acct", now=1100)
        self.assertFalse(ctx.exception.exhausted)
        self.assertEqual(ctx.exception.reason, "cooldown")
        self.state.reserve("acct", now=1300)

    def test_pending_still_blocks_without_quota(self):
        self.state.sync_history("acct", 100, None)
        aid = self.state.reserve("acct", now=1000)
        with self.assertRaises(LimitError) as ctx:
            self.state.reserve("acct", now=1000)
        self.assertEqual(ctx.exception.reason, "in_flight")
        self.assertTrue(ctx.exception.pending)
        self.assertFalse(ctx.exception.exhausted)
        self.state.finish(aid, "success", now=1000)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""submission_state 的单元测试：Python 3.10 标准库 unittest + sqlite3，无第三方依赖、无联网。

运行：
    python -m unittest discover -s tests -v
    python -m unittest tests.test_submission_state -v
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from submission_state import (  # noqa: E402
    MAX_PER_IDENTITY,
    MIN_INTERVAL_SECONDS,
    AttemptError,
    InvalidArgument,
    LimitError,
    StateStore,
    hash_identity,
)


class FakeClock:
    """可控时钟：测试完全不依赖真实时间流逝。"""

    def __init__(self, start=1_700_000_000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


class TempDb:
    """每个用例一个临时 sqlite 文件（:memory: 无法测跨连接并发）。"""

    def __enter__(self):
        fd, self.path = tempfile.mkstemp(prefix="subst_", suffix=".sqlite3")
        os.close(fd)
        os.unlink(self.path)
        return self.path

    def __exit__(self, *exc):
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.unlink(self.path + suffix)
            except OSError:
                pass
        return False


class Base(unittest.TestCase):
    def setUp(self):
        self._db = TempDb()
        self.path = self._db.__enter__()
        self.clock = FakeClock()
        self.store = StateStore(self.path)

    def tearDown(self):
        self._db.__exit__()

    def ident(self, name="openid-aaa"):
        return hash_identity(name)


# --------------------------------------------------------------------------
# 基本行为
# --------------------------------------------------------------------------
class TestBasics(Base):
    def test_hash_identity_stable_and_no_plaintext(self):
        a = hash_identity("oXyz123")
        self.assertEqual(a, hash_identity("oXyz123"))
        self.assertEqual(len(a), 64)
        self.assertNotIn("oXyz123", a)
        self.assertNotEqual(a, hash_identity("oXyz124"))
        with self.assertRaises(InvalidArgument):
            hash_identity("")
        with self.assertRaises(InvalidArgument):
            hash_identity(None)

    def test_status_initial(self):
        st = self.store.status(self.ident(), now=self.clock())
        self.assertEqual(st["used"], 0)
        self.assertEqual(st["remaining"], MAX_PER_IDENTITY)
        self.assertEqual(st["wait_seconds"], 0)
        self.assertFalse(st["blocked"])
        for key in ("used", "remaining", "wait_seconds", "blocked"):
            self.assertIn(key, st)

    def test_blocked_and_pending_are_real_bools(self):
        ident = self.ident()
        st = self.store.status(ident, now=self.clock())
        self.assertIsInstance(st["blocked"], bool)
        self.assertIsInstance(st["pending"], bool)
        self.assertIsInstance(st["exhausted"], bool)
        self.assertIsInstance(st["wait_seconds"], float)
        self.assertFalse(st["blocked"])
        self.assertFalse(st["pending"])
        aid = self.store.reserve(ident, now=self.clock())
        st = self.store.status(ident, now=self.clock())
        self.assertIs(st["blocked"], True)
        self.assertIs(st["pending"], True)
        self.assertIs(st["in_flight"], True)
        self.assertEqual(st["pending_attempt_id"], aid)
        self.assertEqual(st["pending_identity"], ident)
        self.store.finish(aid, "success", now=self.clock())
        st = self.store.status(ident, now=self.clock())
        self.assertIs(st["blocked"], True)    # 冷却中
        self.assertIs(st["pending"], False)
        self.assertIs(st["exhausted"], False)

    def test_cli_gate_sleeps_only_on_wait_without_pending(self):
        """CLI 逻辑：wait_seconds>0 且非 pending 才睡眠，否则停下处理。"""
        ident = self.ident()
        aid = self.store.reserve(ident, now=self.clock())
        # 情况一：pending —— 不能睡眠
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(ident, now=self.clock())
        st = self.store.status(ident, now=self.clock())
        self.assertIs(st["pending"], True)
        self.assertFalse(st["wait_seconds"] > 0 and not st["pending"])
        self.assertIs(ctx.exception.pending, True)
        self.assertEqual(ctx.exception.wait_seconds, 0.0)
        # 情况二：冷却且无 pending —— 可以睡眠
        self.store.finish(aid, "success", now=self.clock())
        with self.assertRaises(LimitError):
            self.store.reserve(ident, now=self.clock())
        st = self.store.status(ident, now=self.clock())
        self.assertIs(st["pending"], False)
        self.assertTrue(st["wait_seconds"] > 0)
        self.assertTrue(st["blocked"])
        self.assertTrue(st["wait_seconds"] > 0 and not st["pending"])
        # 情况三：额度用尽 —— 不该睡眠
        for _ in range(MAX_PER_IDENTITY - 1):
            self.clock.advance(MIN_INTERVAL_SECONDS)
            a = self.store.reserve(ident, now=self.clock())
            self.store.finish(a, "success", now=self.clock())
        self.clock.advance(MIN_INTERVAL_SECONDS)
        with self.assertRaises(LimitError):
            self.store.reserve(ident, now=self.clock())
        st = self.store.status(ident, now=self.clock())
        self.assertIs(st["exhausted"], True)
        self.assertIs(st["blocked"], True)
        self.assertFalse(st["wait_seconds"] > 0 and not st["pending"])

    def test_pending_flag_is_scoped_to_identity(self):
        a, b = self.ident("o1"), self.ident("o2")
        aid = self.store.reserve(a, now=self.clock())
        st_a = self.store.status(a, now=self.clock())
        self.assertIs(st_a["pending"], True)
        self.assertIs(st_a["blocked"], True)
        self.assertEqual(st_a["pending_attempt_id"], aid)
        self.assertEqual(st_a["pending_identity"], a)
        # 另一个身份不受 A 的 pending 影响，可立即提交
        st_b = self.store.status(b, now=self.clock())
        self.assertIs(st_b["pending"], False)
        self.assertIs(st_b["blocked"], False)
        self.assertIsNone(st_b["pending_attempt_id"])
        self.store.reserve(b, now=self.clock())
        self.store.recover_pending(now=self.clock())
        self.assertIs(self.store.status(a, now=self.clock())["pending"], False)

    def test_summary_pending_flag(self):
        ident = self.ident()
        self.store.reserve(ident, now=self.clock())
        s = self.store.summary(now=self.clock())
        self.assertIsInstance(s["in_flight"], bool)
        self.assertIsInstance(s["pending"], list)
        self.assertTrue(s["in_flight"])
        self.assertEqual(len(s["pending"]), 1)

    def test_reserve_returns_attempt_id(self):
        aid = self.store.reserve(self.ident(), now=self.clock())
        self.assertIsInstance(aid, str)
        self.assertTrue(aid)
        self.store.finish(aid, "success", now=self.clock())
        self.clock.advance(MIN_INTERVAL_SECONDS)
        second = self.store.reserve(self.ident("o2"), now=self.clock())
        self.assertIsInstance(second, str)
        self.assertNotEqual(aid, second)

    def test_reserve_consumes_quota_immediately(self):
        ident = self.ident()
        self.store.reserve(ident, now=self.clock())
        st = self.store.status(ident, now=self.clock())
        self.assertEqual(st["used"], 1)
        self.assertEqual(st["remaining"], MAX_PER_IDENTITY - 1)

    def test_ten_submissions_then_exhausted(self):
        ident = self.ident()
        for i in range(MAX_PER_IDENTITY):
            aid = self.store.reserve(ident, now=self.clock())
            self.store.finish(aid, "success", now=self.clock())
            self.clock.advance(MIN_INTERVAL_SECONDS)
        st = self.store.status(ident, now=self.clock())
        self.assertEqual(st["used"], MAX_PER_IDENTITY)
        self.assertEqual(st["remaining"], 0)
        self.assertTrue(st["exhausted"])
        self.assertTrue(st["blocked"])
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(ident, now=self.clock())
        self.assertTrue(ctx.exception.exhausted)
        self.assertEqual(ctx.exception.reason, "exhausted")

    def test_status_default_now_uses_monotonic_real_clock(self):
        ident = self.ident()
        aid = self.store.reserve(ident)
        self.store.finish(aid, "success")
        st = self.store.status(ident)
        self.assertEqual(st["used"], 1)
        self.assertGreater(st["wait_seconds"], 0)
        self.assertTrue(st["blocked"])


# --------------------------------------------------------------------------
# 冷却 / 间隔
# --------------------------------------------------------------------------
class TestCooldown(Base):
    def test_interval_enforced_per_identity(self):
        ident = self.ident()
        aid = self.store.reserve(ident, now=self.clock())
        self.store.finish(aid, "success", now=self.clock())
        self.clock.advance(MIN_INTERVAL_SECONDS - 1)
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(ident, now=self.clock())
        exc = ctx.exception
        self.assertFalse(exc.exhausted)
        self.assertEqual(exc.reason, "cooldown")
        self.assertGreaterEqual(exc.wait_seconds, 1)
        self.clock.advance(1)
        self.store.reserve(ident, now=self.clock())

    def test_switching_identity_submits_immediately(self):
        """切号即可提交：A 的冷却不约束 B。"""
        a, b = self.ident("o1"), self.ident("o2")
        aid = self.store.reserve(a, now=self.clock())
        self.store.finish(aid, "success", now=self.clock())
        self.clock.advance(MIN_INTERVAL_SECONDS - 5)
        st_b = self.store.status(b, now=self.clock())
        self.assertEqual(st_b["wait_seconds"], 0)
        self.assertFalse(st_b["blocked"])
        self.store.reserve(b, now=self.clock())
        # A 自己仍受 300 秒间隔约束
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(a, now=self.clock())
        self.assertEqual(ctx.exception.reason, "cooldown")
        self.assertGreaterEqual(ctx.exception.wait_seconds, 5)
        self.clock.advance(5)
        self.store.reserve(a, now=self.clock())

    def test_fractional_advance_still_blocked(self):
        ident = self.ident()
        aid = self.store.reserve(ident, now=self.clock())
        self.store.finish(aid, "success", now=self.clock())
        self.clock.advance(MIN_INTERVAL_SECONDS - 0.5)
        with self.assertRaises(LimitError):
            self.store.reserve(ident, now=self.clock())
        self.clock.advance(0.5)
        self.store.reserve(ident, now=self.clock())

    def test_wait_seconds_counts_from_finish_not_reserve(self):
        ident = self.ident()
        aid = self.store.reserve(ident, now=self.clock())
        self.clock.advance(200)
        self.store.finish(aid, "success", now=self.clock())
        self.clock.advance(200)
        st = self.store.status(ident, now=self.clock())
        self.assertGreater(st["wait_seconds"], 0)
        with self.assertRaises(LimitError):
            self.store.reserve(ident, now=self.clock())
        self.clock.advance(100)
        self.store.reserve(ident, now=self.clock())


# --------------------------------------------------------------------------
# 别名：同一个散列 identity 不重置
# --------------------------------------------------------------------------
class TestAlias(Base):
    def test_alias_same_identity_never_resets_quota(self):
        ident = hash_identity("oREAL_OPENID")
        self.assertEqual(ident, hash_identity("oREAL_OPENID"))
        for nickname in ("小明", "Ming", "小明 ", "xiaoming"):
            self.assertNotEqual(ident, hash_identity(nickname))
        with self.assertRaises(InvalidArgument):
            hash_identity("")  # 空别名不能当作身份
        for i in range(MAX_PER_IDENTITY):
            aid = self.store.reserve(ident, now=self.clock())
            self.store.finish(aid, "success", now=self.clock())
            self.clock.advance(MIN_INTERVAL_SECONDS)
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(ident, now=self.clock())
        self.assertTrue(ctx.exception.exhausted)
        self.assertEqual(self.store.status(ident, now=self.clock())["used"], MAX_PER_IDENTITY)

    def test_alias_cannot_escape_cooldown(self):
        """同一个 openid 换别名/换回仍受剩余额度与冷却约束。"""
        ident = hash_identity("oREAL")
        aid = self.store.reserve(ident, now=self.clock())
        self.store.finish(aid, "success", now=self.clock())
        self.clock.advance(10)
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(hash_identity("oREAL"), now=self.clock())
        self.assertEqual(ctx.exception.reason, "cooldown")
        self.assertEqual(self.store.status(hash_identity("oREAL"), now=self.clock())["used"], 1)
        # 换回后冷却到期仍可继续，但额度不重置
        self.clock.advance(MIN_INTERVAL_SECONDS)
        self.store.reserve(hash_identity("oREAL"), now=self.clock())
        self.assertEqual(self.store.status(ident, now=self.clock())["used"], 2)
        # 另一个 openid 是不同的身份，不受本身份冷却影响
        self.store.reserve(hash_identity("oOTHER_ACCOUNT"), now=self.clock())

    def test_cli_alias_labels_map_to_same_identity(self):
        """不同本地别名指向同一 openid：额度与冷却共用，不因换标签重置。"""
        openid = "oSAME_ACCOUNT"
        labels = {"wx-1": hash_identity(openid), "wx-2": hash_identity(openid)}
        self.assertEqual(labels["wx-1"], labels["wx-2"])
        aid = self.store.reserve(labels["wx-1"], now=self.clock())
        self.store.finish(aid, "success", now=self.clock())
        self.clock.advance(10)
        # 换标签提交仍被同一身份的冷却拦住
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(labels["wx-2"], now=self.clock())
        self.assertEqual(ctx.exception.reason, "cooldown")
        self.assertEqual(self.store.status(labels["wx-1"], now=self.clock())["used"], 1)
        self.clock.advance(MIN_INTERVAL_SECONDS)
        self.store.reserve(labels["wx-2"], now=self.clock())
        self.assertEqual(self.store.status(labels["wx-1"], now=self.clock())["used"], 2)
        # 不同 openid 才是不同身份
        other = hash_identity("oANOTHER_ACCOUNT")
        self.assertNotEqual(other, labels["wx-1"])
        self.assertEqual(self.store.status(other, now=self.clock())["wait_seconds"], 0)
        self.store.reserve(other, now=self.clock())

    def test_different_identities_have_separate_quota_and_cooldown(self):
        a, b = self.ident("o1"), self.ident("o2")
        for _ in range(3):
            aid = self.store.reserve(a, now=self.clock())
            self.store.finish(aid, "success", now=self.clock())
            self.clock.advance(MIN_INTERVAL_SECONDS)
        self.assertEqual(self.store.status(a, now=self.clock())["used"], 3)
        self.assertEqual(self.store.status(b, now=self.clock())["used"], 0)
        self.assertEqual(self.store.status(b, now=self.clock())["remaining"], MAX_PER_IDENTITY)
        self.assertEqual(self.store.status(b, now=self.clock())["wait_seconds"], 0)
        self.store.reserve(b, now=self.clock())  # B 不受 A 的冷却影响


# --------------------------------------------------------------------------
# 远端历史同步
# --------------------------------------------------------------------------
class TestSyncHistory(Base):
    def test_used_takes_max_of_local_and_remote(self):
        ident = self.ident()
        self.store.sync_history(ident, 7, 1_600_000_000.0)
        st = self.store.status(ident, now=self.clock())
        self.assertEqual(st["used"], 7)
        self.assertEqual(st["remaining"], 3)
        self.assertEqual(st["last_at"], 1_600_000_000.0)

    def test_smaller_remote_does_not_rewind(self):
        ident = self.ident()
        self.store.sync_history(ident, 7, 1_600_000_000.0)
        self.store.sync_history(ident, 2, 1_500_000_000.0)
        st = self.store.status(ident, now=self.clock())
        self.assertEqual(st["used"], 7)
        self.assertEqual(st["last_at"], 1_600_000_000.0)

    def test_sync_beyond_quota_blocks(self):
        ident = self.ident()
        self.store.sync_history(ident, MAX_PER_IDENTITY, None)
        st = self.store.status(ident, now=self.clock())
        self.assertTrue(st["exhausted"])
        self.assertTrue(st["blocked"])
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(ident, now=self.clock())
        self.assertTrue(ctx.exception.exhausted)

    def test_sync_last_at_starts_cooldown(self):
        ident = self.ident()
        self.store.sync_history(ident, 1, self.clock.now - 100)
        st = self.store.status(ident, now=self.clock())
        self.assertGreater(st["wait_seconds"], 0)
        with self.assertRaises(LimitError):
            self.store.reserve(ident, now=self.clock())
        self.clock.advance(MIN_INTERVAL_SECONDS)
        self.store.reserve(ident, now=self.clock())

    def test_sync_history_keeps_global_stat_only(self):
        """远端历史只约束同步的那个身份，不给别号制造冷却。"""
        a, b = self.ident("o1"), self.ident("o2")
        self.store.sync_history(a, 1, self.clock.now - 100)
        st_b = self.store.status(b, now=self.clock())
        self.assertIs(st_b["pending"], False)
        self.assertEqual(st_b["wait_seconds"], 0)
        self.assertFalse(st_b["blocked"])
        self.store.reserve(b, now=self.clock())
        # 仅统计字段保留，不参与判定
        self.assertIsNotNone(st_b["global_last_at"])
        # A 自己被远端记录约束
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(a, now=self.clock())
        self.assertEqual(ctx.exception.reason, "cooldown")

    def test_sync_history_global_stat_monotonic_and_persistent(self):
        a, b = self.ident("o1"), self.ident("o2")
        self.store.sync_history(a, 1, self.clock.now)
        self.store.sync_history(a, 1, self.clock.now - 1000)
        st = self.store.status(b, now=self.clock())
        self.assertAlmostEqual(st["global_last_at"], self.clock.now, places=6)
        self.assertEqual(st["wait_seconds"], 0)
        reopened = StateStore(self.path)
        self.assertEqual(reopened.status(b, now=self.clock())["wait_seconds"], 0)
        self.assertGreater(reopened.status(a, now=self.clock())["wait_seconds"], 0)

    def test_sync_history_without_last_at_leaves_others_untouched(self):
        b = self.ident("o2")
        self.store.sync_history(self.ident("o1"), 5, None)
        self.assertIsNone(self.store.status(b, now=self.clock())["global_last_at"])
        self.assertEqual(self.store.status(b, now=self.clock())["wait_seconds"], 0)
        self.store.reserve(b, now=self.clock())

    def test_sync_validates_arguments(self):
        ident = self.ident()
        with self.assertRaises(InvalidArgument):
            self.store.sync_history(ident, -1)
        with self.assertRaises(InvalidArgument):
            self.store.sync_history(ident, 1.5)
        with self.assertRaises(InvalidArgument):
            self.store.sync_history(ident, True)
        with self.assertRaises(InvalidArgument):
            self.store.sync_history(ident, "3")
        with self.assertRaises(InvalidArgument):
            self.store.sync_history(ident, 1, float("nan"))
        with self.assertRaises(InvalidArgument):
            self.store.sync_history(ident, 1, float("inf"))
        with self.assertRaises(InvalidArgument):
            self.store.sync_history(ident, 1, "1700000000")
        with self.assertRaises(InvalidArgument):
            self.store.sync_history(ident, 1, True)
        with self.assertRaises(InvalidArgument):
            self.store.sync_history("", 1)
        with self.assertRaises(InvalidArgument):
            self.store.sync_history(None, 1)
        with self.assertRaises(InvalidArgument):
            self.store.sync_history(b"x", 1)

    def test_sync_history_does_not_clear_pending(self):
        ident = self.ident()
        aid = self.store.reserve(ident, now=self.clock())
        self.store.sync_history(ident, 3, self.clock.now)
        st = self.store.status(ident, now=self.clock())
        self.assertTrue(st["in_flight"])
        self.assertEqual(st["pending_attempt_id"], aid)
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(ident, now=self.clock())
        self.assertEqual(ctx.exception.reason, "in_flight")
        # 远端已确认的次数不能把已预留的额度抹掉
        self.assertEqual(st["used"], 3)

    def test_count_non_negative_and_finite_last_at_accepted(self):
        ident = self.ident()
        out = self.store.sync_history(ident, 0, None)
        self.assertEqual(out["used"], 0)
        self.assertIsNone(out["last_at"])
        out = self.store.sync_history(ident, 5, 1_600_000_000.5)
        self.assertEqual(out["used"], 5)
        self.assertEqual(out["last_at"], 1_600_000_000.5)


# --------------------------------------------------------------------------
# 失败 / 不确定
# --------------------------------------------------------------------------
class TestFailureAndUnknown(Base):
    def test_rejected_consumes_quota_and_cools_down(self):
        ident = self.ident()
        aid = self.store.reserve(ident, now=self.clock())
        self.store.finish(aid, "rejected", now=self.clock())
        st = self.store.status(ident, now=self.clock())
        self.assertEqual(st["used"], 1)
        self.assertGreater(st["wait_seconds"], 0)
        with self.assertRaises(LimitError):
            self.store.reserve(ident, now=self.clock())

    def test_unknown_consumes_quota_and_cools_down(self):
        ident = self.ident()
        aid = self.store.reserve(ident, now=self.clock())
        self.store.finish(aid, "unknown", now=self.clock())
        st = self.store.status(ident, now=self.clock())
        self.assertEqual(st["used"], 1)
        self.assertGreater(st["wait_seconds"], 0)

    def test_no_automatic_refund_after_ten_failures(self):
        ident = self.ident()
        for _ in range(MAX_PER_IDENTITY):
            aid = self.store.reserve(ident, now=self.clock())
            self.store.finish(aid, "rejected", now=self.clock())
            self.clock.advance(MIN_INTERVAL_SECONDS)
        st = self.store.status(ident, now=self.clock())
        self.assertEqual(st["used"], MAX_PER_IDENTITY)
        self.assertTrue(st["exhausted"])
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(ident, now=self.clock())
        self.assertTrue(ctx.exception.exhausted)

    def test_finish_validates(self):
        ident = self.ident()
        aid = self.store.reserve(ident, now=self.clock())
        with self.assertRaises(InvalidArgument):
            self.store.finish(aid, "SUCCESS", now=self.clock())
        with self.assertRaises(InvalidArgument):
            self.store.finish(aid, "pending", now=self.clock())
        with self.assertRaises(InvalidArgument):
            self.store.finish(aid, "", now=self.clock())
        with self.assertRaises(InvalidArgument):
            self.store.finish("", "success")
        with self.assertRaises(InvalidArgument):
            self.store.finish(None, "success")
        with self.assertRaises(AttemptError):
            self.store.finish("no-such-attempt", "success", now=self.clock())
        self.store.finish(aid, "success", now=self.clock())
        with self.assertRaises(AttemptError):
            self.store.finish(aid, "success", now=self.clock())
        with self.assertRaises(AttemptError):
            self.store.finish(aid, "unknown", now=self.clock())
        with self.assertRaises(InvalidArgument):
            self.store.finish(aid, "success", now=float("nan"))

    def test_failed_attempt_leaves_no_in_flight(self):
        ident = self.ident()
        aid = self.store.reserve(ident, now=self.clock())
        self.store.finish(aid, "unknown", now=self.clock())
        st = self.store.status(ident, now=self.clock())
        self.assertFalse(st["in_flight"])
        self.assertIsNone(st["pending_attempt_id"])


# --------------------------------------------------------------------------
# in_flight / 崩溃恢复
# --------------------------------------------------------------------------
class TestInFlightAndRecover(Base):
    def test_concurrent_reserve_blocked_per_identity_not_global(self):
        ident = self.ident()
        other = self.ident("other")
        aid = self.store.reserve(ident, now=self.clock())
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(ident, now=self.clock())
        self.assertEqual(ctx.exception.reason, "in_flight")
        self.assertEqual(ctx.exception.identity, ident)
        # 另一个身份不受该 pending 影响
        self.store.reserve(other, now=self.clock())
        self.store.finish(aid, "success", now=self.clock())

    def test_crash_left_pending_is_not_auto_retried(self):
        ident = self.ident()
        self.store.reserve(ident, now=self.clock())
        self.store = StateStore(self.path)  # 模拟进程重启
        st = self.store.status(ident, now=self.clock())
        self.assertTrue(st["in_flight"])
        self.assertEqual(st["used"], 1)
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(ident, now=self.clock())
        self.assertEqual(ctx.exception.reason, "in_flight")
        self.assertIn("recover_pending", str(ctx.exception))

    def test_recover_pending_marks_unknown_and_cools(self):
        ident = self.ident()
        aid = self.store.reserve(ident, now=self.clock())
        self.clock.advance(10)
        recovered = self.store.recover_pending(now=self.clock())
        self.assertEqual(recovered, [aid])
        st = self.store.status(ident, now=self.clock())
        self.assertFalse(st["in_flight"])
        self.assertEqual(st["used"], 1)
        self.assertEqual(st["last_at"], self.clock.now)
        self.assertGreater(st["wait_seconds"], 0)
        self.clock.advance(MIN_INTERVAL_SECONDS)
        second = self.store.reserve(ident, now=self.clock())
        self.assertNotEqual(second, aid)

    def test_recover_pending_needs_full_300s_from_now(self):
        ident = self.ident()
        self.store.reserve(ident, now=self.clock())
        self.clock.advance(299)
        self.store.recover_pending(now=self.clock())
        self.clock.advance(299)
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(ident, now=self.clock())
        self.assertEqual(ctx.exception.reason, "cooldown")
        self.clock.advance(1)
        self.store.reserve(ident, now=self.clock())

    def test_recover_pending_twice_is_noop(self):
        ident = self.ident()
        self.store.reserve(ident, now=self.clock())
        self.assertEqual(len(self.store.recover_pending(now=self.clock())), 1)
        self.assertEqual(self.store.recover_pending(now=self.clock()), [])

    def test_recover_pending_without_argument_uses_real_clock(self):
        ident = self.ident()
        aid = self.store.reserve(ident)
        self.assertEqual(self.store.recover_pending(), [aid])
        st = self.store.status(ident)
        self.assertFalse(st["in_flight"])
        self.assertGreater(st["wait_seconds"], 0)

    def test_recover_pending_no_pending_returns_empty(self):
        self.assertEqual(self.store.recover_pending(now=self.clock()), [])

    def test_recover_pending_cools_only_own_identity(self):
        a, b = self.ident("o1"), self.ident("o2")
        self.store.reserve(a, now=self.clock())
        self.store.recover_pending(now=self.clock())
        # B 不受 A 恢复出的冷却影响
        self.assertEqual(self.store.status(b, now=self.clock())["wait_seconds"], 0)
        self.store.reserve(b, now=self.clock())
        # A 自己需要等满 300 秒
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(a, now=self.clock())
        self.assertEqual(ctx.exception.reason, "cooldown")
        self.assertEqual(self.store.status(a, now=self.clock())["used"], 1)

    def test_pending_listing(self):
        ident = self.ident()
        aid = self.store.reserve(ident, now=self.clock())
        pend = self.store.pending(now=self.clock())
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0]["attempt_id"], aid)
        self.assertEqual(pend[0]["identity"], ident)
        self.store.recover_pending(now=self.clock())
        self.assertEqual(self.store.pending(now=self.clock()), [])


# --------------------------------------------------------------------------
# 持久化 / 重启
# --------------------------------------------------------------------------
class TestPersistence(Base):
    def test_state_survives_restart(self):
        ident = self.ident()
        for _ in range(4):
            aid = self.store.reserve(ident, now=self.clock())
            self.store.finish(aid, "success", now=self.clock())
            self.clock.advance(MIN_INTERVAL_SECONDS)
        reopened = StateStore(self.path)
        st = reopened.status(ident, now=self.clock())
        self.assertEqual(st["used"], 4)
        self.assertEqual(st["remaining"], MAX_PER_IDENTITY - 4)
        self.assertAlmostEqual(st["last_at"], self.clock.now - MIN_INTERVAL_SECONDS, places=6)

    def test_new_instance_reads_same_file(self):
        ident = self.ident()
        aid = self.store.reserve(ident, now=self.clock())
        other = StateStore(self.path)
        self.assertTrue(other.status(ident, now=self.clock())["in_flight"])
        other.finish(aid, "success", now=self.clock())
        self.assertFalse(self.store.status(ident, now=self.clock())["in_flight"])
        self.assertEqual(self.store.status(ident, now=self.clock())["used"], 1)

    def test_no_openid_stored_in_db(self):
        raw = "oSUPER_SECRET_OPENID_123"
        ident = hash_identity(raw)
        aid = self.store.reserve(ident, now=self.clock())
        self.store.finish(aid, "success", now=self.clock())
        conn = sqlite3.connect(self.path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            dump = []
            for (name,) in rows:
                dump.extend(str(r) for r in conn.execute("SELECT * FROM %s" % name).fetchall())
        finally:
            conn.close()
        self.assertTrue(dump)
        for chunk in dump:
            self.assertNotIn(raw, chunk)
        self.assertIn(ident, "\n".join(dump))


# --------------------------------------------------------------------------
# 时钟回拨
# --------------------------------------------------------------------------
class TestClockSkew(Base):
    def test_backwards_clock_does_not_release_early(self):
        ident = self.ident()
        aid = self.store.reserve(ident, now=self.clock())
        self.store.finish(aid, "success", now=self.clock())
        good_now = self.clock.advance(MIN_INTERVAL_SECONDS + 60)
        self.assertEqual(self.store.status(ident, now=good_now)["wait_seconds"], 0)
        self.clock.advance(-10_000)  # 系统时间被回拨到冷却窗口之内
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(ident, now=self.clock())
        self.assertEqual(ctx.exception.reason, "cooldown")
        self.assertGreater(ctx.exception.wait_seconds, 0)
        st = self.store.status(ident, now=self.clock())
        self.assertTrue(st["blocked"])
        self.assertGreater(st["wait_seconds"], 0)
        # 时钟恢复正常（或只是比见过的最晚时刻更晚）后必须放行
        self.assertTrue(self.store.reserve(ident, now=good_now))

    def test_large_backwards_clock_blocks_again(self):
        ident = self.ident()
        aid = self.store.reserve(ident, now=self.clock())
        self.store.finish(aid, "success", now=self.clock())
        real_now = self.clock.advance(MIN_INTERVAL_SECONDS + 600)
        self.clock.advance(-1_000_000)
        with self.assertRaises(LimitError):
            self.store.reserve(ident, now=self.clock())
        self.store.reserve(ident, now=real_now)

    def test_monotonic_guard_persists_across_restart(self):
        ident = self.ident()
        aid = self.store.reserve(ident, now=self.clock())
        self.store.finish(aid, "success", now=self.clock())
        far = self.clock.advance(MIN_INTERVAL_SECONDS + 120)
        reopened = StateStore(self.path)
        with self.assertRaises(LimitError):
            reopened.reserve(ident, now=far - 100_000)
        reopened.reserve(ident, now=far)


# --------------------------------------------------------------------------
# 并发 / 多连接（BEGIN IMMEDIATE）
# --------------------------------------------------------------------------
class TestConcurrency(Base):
    def test_threads_no_double_spend(self):
        ident = self.ident()
        clock = FakeClock()
        results, errors = [], []

        def worker():
            try:
                results.append(self.store.reserve(ident, now=clock()))
            except LimitError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 23)
        for exc in errors:
            self.assertEqual(exc.reason, "in_flight")
        self.assertEqual(self.store.status(ident, now=clock())["used"], 1)

    def test_multi_connection_single_winner(self):
        ident = self.ident()
        stores = [StateStore(self.path) for _ in range(8)]
        clock = FakeClock()
        wins, losses = [], []
        for s in stores:
            try:
                wins.append(s.reserve(ident, now=clock()))
            except LimitError as exc:
                losses.append(exc.reason)
        self.assertEqual(len(wins), 1)
        self.assertEqual(set(losses), {"in_flight"})
        for s in stores:
            self.assertEqual(s.status(ident, now=clock())["used"], 1)

    def test_multi_connection_quota_not_exceeded(self):
        ident = self.ident()
        stores = [StateStore(self.path) for _ in range(6)]
        clock = FakeClock()
        accepted = 0
        for _ in range(MAX_PER_IDENTITY * 3):
            for s in stores:
                try:
                    aid = s.reserve(ident, now=clock())
                except LimitError:
                    continue
                accepted += 1
                s.finish(aid, "success", now=clock())
                clock.advance(MIN_INTERVAL_SECONDS)
        self.assertEqual(accepted, MAX_PER_IDENTITY)
        self.assertEqual(self.store.status(ident, now=clock())["used"], MAX_PER_IDENTITY)

    def test_parallel_finish_only_first_wins(self):
        ident = self.ident()
        clock = FakeClock()
        aid = self.store.reserve(ident, now=clock())
        stores = [StateStore(self.path) for _ in range(6)]
        ok, bad = [], []
        lock = threading.Lock()

        def worker(s):
            try:
                s.finish(aid, "success", now=clock())
                with lock:
                    ok.append(s)
            except AttemptError:
                with lock:
                    bad.append(s)

        threads = [threading.Thread(target=worker, args=(s,)) for s in stores]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(bad), 5)


# --------------------------------------------------------------------------
# 参数 / 接口约定
# --------------------------------------------------------------------------
class TestApiContract(Base):
    def test_no_tunable_knobs(self):
        self.assertEqual(MAX_PER_IDENTITY, 10)
        self.assertEqual(MIN_INTERVAL_SECONDS, 300)
        self.assertEqual(self.store.max_per_identity, 10)
        self.assertEqual(self.store.min_interval, 300)
        self.assertFalse(hasattr(self.store, "set_max_per_identity"))
        self.assertFalse(hasattr(self.store, "set_interval"))

    def test_reserve_validates(self):
        with self.assertRaises(InvalidArgument):
            self.store.reserve("", now=self.clock())
        with self.assertRaises(InvalidArgument):
            self.store.reserve(None, now=self.clock())
        with self.assertRaises(InvalidArgument):
            self.store.reserve(123, now=self.clock())
        with self.assertRaises(InvalidArgument):
            self.store.reserve(self.ident(), now=float("nan"))
        with self.assertRaises(InvalidArgument):
            self.store.reserve(self.ident(), now=float("inf"))
        with self.assertRaises(InvalidArgument):
            self.store.reserve(self.ident(), now=True)
        with self.assertRaises(InvalidArgument):
            self.store.status(self.ident(), now="1700000000")
        with self.assertRaises(InvalidArgument):
            self.store.recover_pending(now="x")

    def test_limit_error_attributes(self):
        ident = self.ident()
        aid = self.store.reserve(ident, now=self.clock())
        self.store.finish(aid, "success", now=self.clock())
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(ident, now=self.clock())
        exc = ctx.exception
        self.assertIsInstance(exc.wait_seconds, float)
        self.assertFalse(exc.exhausted)
        self.assertFalse(exc.pending)
        self.assertEqual(exc.identity, ident)
        self.assertIsInstance(str(exc), str)
        self.assertTrue(str(exc))

    def test_limit_error_pending_flag_is_per_identity(self):
        ident = self.ident()
        self.store.reserve(ident, now=self.clock())
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(ident, now=self.clock())
        exc = ctx.exception
        self.assertEqual(exc.reason, "in_flight")
        self.assertIs(exc.pending, True)
        self.assertEqual(exc.wait_seconds, 0.0)
        self.assertEqual(exc.identity, ident)
        # 另一个身份没有任何拦截
        self.store.reserve(self.ident("fresh"), now=self.clock())

    def test_limit_error_defaults(self):
        exc = LimitError("测试")
        self.assertEqual(exc.wait_seconds, 0.0)
        self.assertFalse(exc.exhausted)
        self.assertEqual(str(exc), "测试")

    def test_context_manager(self):
        with StateStore(self.path) as store:
            ident = self.ident()
            aid = store.reserve(ident, now=self.clock())
            store.finish(aid, "success", now=self.clock())
            self.assertEqual(store.status(ident, now=self.clock())["used"], 1)

    def test_summary(self):
        a, b = self.ident("o1"), self.ident("o2")
        aid = self.store.reserve(a, now=self.clock())
        self.store.finish(aid, "success", now=self.clock())
        self.clock.advance(MIN_INTERVAL_SECONDS)
        self.store.sync_history(b, 4, None)
        s = self.store.summary(now=self.clock())
        self.assertEqual(s["identity_count"], 2)
        self.assertEqual(s["total_used"], 5)
        self.assertEqual(s["total_remaining"], MAX_PER_IDENTITY * 2 - 5)
        self.assertEqual(s["quota"], MAX_PER_IDENTITY)
        self.assertEqual(s["interval"], MIN_INTERVAL_SECONDS)
        self.assertFalse(s["in_flight"])
        self.assertEqual(s["pending"], [])
        self.assertEqual(s["identities"][a]["used"], 1)
        self.assertEqual(s["identities"][b]["used"], 4)

    def test_error_messages_are_chinese(self):
        ident = self.ident()
        aid = self.store.reserve(ident, now=self.clock())
        self.store.finish(aid, "success", now=self.clock())
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(ident, now=self.clock())
        self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in str(ctx.exception)))
        for _ in range(MAX_PER_IDENTITY - 1):
            self.clock.advance(MIN_INTERVAL_SECONDS)
            a = self.store.reserve(ident, now=self.clock())
            self.store.finish(a, "success", now=self.clock())
        self.clock.advance(MIN_INTERVAL_SECONDS)
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(ident, now=self.clock())
        self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in str(ctx.exception)))


# --------------------------------------------------------------------------
# 切换账号：资格/冷却/pending 完全按 identity 划分
# --------------------------------------------------------------------------
class TestPerIdentitySwitching(Base):
    def test_b_submits_immediately_after_a_finishes(self):
        a, b = self.ident("oA"), self.ident("oB")
        aid = self.store.reserve(a, now=1000)
        self.store.finish(aid, "success", now=1000)
        self.store.reserve(b, now=1000)      # B 不受 A 的 300 秒约束

    def test_a_blocked_at_1299_and_allowed_at_1300(self):
        a, b = self.ident("oA"), self.ident("oB")
        aid = self.store.reserve(a, now=1000)
        self.store.finish(aid, "success", now=1000)
        self.store.reserve(b, now=1000)      # 切号立即提交
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(a, now=1299)  # A 自己仍需 300 秒
        self.assertEqual(ctx.exception.reason, "cooldown")
        self.assertEqual(ctx.exception.wait_seconds, 1)
        self.store.reserve(a, now=1300)

    def test_a_pending_does_not_block_b(self):
        a, b = self.ident("oA"), self.ident("oB")
        self.store.reserve(a, now=1000)      # A 留下未结束提交（模拟崩溃）
        self.store.reserve(b, now=1000)
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(a, now=1000)
        self.assertEqual(ctx.exception.reason, "in_flight")

    def test_sync_history_of_a_does_not_block_b(self):
        a, b = self.ident("oA"), self.ident("oB")
        self.store.sync_history(a, 1, 1000)  # 远端已知 A 刚提交
        self.store.reserve(b, now=1000)
        with self.assertRaises(LimitError) as ctx:
            self.store.reserve(a, now=1000)
        self.assertEqual(ctx.exception.reason, "cooldown")
        self.assertEqual(self.store.status(b, now=1000)["wait_seconds"], 0)

    def test_stale_future_global_stat_does_not_block_new_identity(self):
        """旧库里遗留的未来 global_state.last_at 不能阻塞新账号。"""
        b = self.ident("oB")
        db = sqlite3.connect(self.path)
        try:
            db.execute("UPDATE global_state SET last_at=? WHERE id=1", (10 ** 9,))
            db.commit()
        finally:
            db.close()
        st = self.store.status(b, now=1000)
        self.assertEqual(st["wait_seconds"], 0)
        self.assertFalse(st["blocked"])
        self.store.reserve(b, now=1000)

    def test_old_data_keeps_counts_and_per_identity_cooldown(self):
        """重启/新实例：旧数据不重置；冷却只约束提交过的那个身份。"""
        a, b = self.ident("oA"), self.ident("oB")
        base = 1000.0
        for i in range(4):
            at = base + i * MIN_INTERVAL_SECONDS
            aid = self.store.reserve(a, now=at)
            self.store.finish(aid, "success", now=at)
        last_at = base + 3 * MIN_INTERVAL_SECONDS      # 第 4 次完成时刻
        now = last_at + 60                             # 仍在 A 的 300 秒冷却内
        reopened = StateStore(self.path)
        self.assertEqual(reopened.status(a, now=now)["used"], 4)
        self.assertEqual(reopened.status(b, now=now)["used"], 0)
        reopened.reserve(b, now=now)                   # B 立即可用
        self.assertEqual(reopened.status(b, now=now)["used"], 1)
        with self.assertRaises(LimitError) as ctx:
            reopened.reserve(a, now=now)               # A 仍冷却
        self.assertEqual(ctx.exception.reason, "cooldown")
        self.assertEqual(ctx.exception.wait_seconds, MIN_INTERVAL_SECONDS - 60)
        self.assertEqual(reopened.status(a, now=now)["used"], 4)
        # 冷却结束后 A 继续，次数不重置
        reopened.reserve(a, now=last_at + MIN_INTERVAL_SECONDS)
        self.assertEqual(reopened.status(a, now=last_at + MIN_INTERVAL_SECONDS)["used"], 5)

    def test_summary_blocked_is_per_identity(self):
        a, b = self.ident("oA"), self.ident("oB")
        # 两个身份都先落库，保证出现在 summary 的 identities 里
        self.store.reserve(b, now=1000)
        self.store.finish(self.store.status(b, now=1000)["pending_attempt_id"], "success", now=1000)
        aid = self.store.reserve(a, now=1000)
        self.store.finish(aid, "success", now=1000)
        s = self.store.summary(now=1000)
        self.assertIn(a, s["identities"])
        self.assertIn(b, s["identities"])
        self.assertTrue(s["identities"][a]["blocked"])   # A 刚提交，冷却中
        self.assertTrue(s["identities"][b]["blocked"])   # B 也刚提交
        # A 冷却结束后只有 A 可用；B 仍在自己的冷却里（互相独立）
        self.clock.advance(MIN_INTERVAL_SECONDS)
        s2 = self.store.summary(now=1000 + MIN_INTERVAL_SECONDS)
        self.assertFalse(s2["identities"][a]["blocked"])
        self.assertFalse(s2["identities"][b]["blocked"])
        # pending 只标记自己的身份
        self.store.reserve(b, now=1000 + MIN_INTERVAL_SECONDS)
        s3 = self.store.summary(now=1000 + MIN_INTERVAL_SECONDS)
        self.assertTrue(s3["identities"][b]["pending"])
        self.assertFalse(s3["identities"][a]["pending"])
        self.assertTrue(s3["identities"][b]["blocked"])
        self.assertFalse(s3["identities"][a]["blocked"])

    def test_threads_single_winner_per_identity_and_parallel_identities(self):
        a, b = self.ident("oA"), self.ident("oB")
        clock = FakeClock()
        wins_a, wins_b, errors = [], [], []
        lock = threading.Lock()

        def worker(ident, bucket):
            try:
                aid = self.store.reserve(ident, now=clock())
                with lock:
                    bucket.append(aid)
            except LimitError as exc:
                with lock:
                    errors.append(exc)

        threads = []
        for _ in range(12):
            threads.append(threading.Thread(target=worker, args=(a, wins_a)))
            threads.append(threading.Thread(target=worker, args=(b, wins_b)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(wins_a), 1)      # 同 identity 原子预留只有一个
        self.assertEqual(len(wins_b), 1)      # 另一个 identity 并行成功
        self.assertEqual(len(errors), 22)
        self.assertEqual(self.store.status(a, now=clock())["used"], 1)
        self.assertEqual(self.store.status(b, now=clock())["used"], 1)

    def test_multi_connection_single_winner_per_identity(self):
        """多连接并发：同一 identity 只有一个 winner；其他身份不受影响。"""
        a = self.ident("oA")
        fresh = self.ident("oC")          # 尚未 pending 的身份
        clock = FakeClock()
        stores = [StateStore(self.path) for _ in range(8)]
        wins, losses = [], []
        for s in stores:
            try:
                wins.append(s.reserve(a, now=clock()))
            except LimitError as exc:
                losses.append(exc.reason)
        self.assertEqual(len(wins), 1)
        self.assertEqual(set(losses), {"in_flight"})
        # 同一库下其他身份不受 A 的 pending 影响
        other = StateStore(self.path)
        self.assertTrue(other.reserve(fresh, now=clock()))
        # 已 pending 的身份 C 再预留会被 in_flight 拒绝（不重复提交）
        with self.assertRaises(LimitError) as ctx:
            other.reserve(fresh, now=clock())
        self.assertEqual(ctx.exception.reason, "in_flight")
        # 且额度未被重复扣减
        self.assertEqual(other.status(fresh, now=clock())["used"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

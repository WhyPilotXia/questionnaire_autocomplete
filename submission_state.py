# -*- coding: utf-8 -*-
"""问卷提交状态库（submission_state）。

职责：把"同一微信身份两次提交至少间隔 300 秒"这条规则落到本地 SQLite 里，
并保证跨进程、跨重启、面对失败/不确定结果时都保守。**不再限制累计提交次数**
（历史 21 次、100 次也可以继续提交），used 仅用于日志展示与 pending 自动核对。

设计要点
--------
1. 每个微信身份（调用方传入的**散列** identity，绝不保存 openid 原文）
   **没有累计次数上限**；reserve() 仍会累加 used（失败或不确定也不退款、
   不自动重试，保守），但 used 不再参与资格判定。
2. 同一身份两次提交至少间隔 MIN_INTERVAL_SECONDS 秒。冷却与资格判定
   **完全按 identity 划分**：A 账号刚提交过不影响 B 账号立即提交；切换
   不同微信号可以立刻填答，同一微信号只受 300 秒间隔约束。
3. 冷却的起点是"提交完成时间"（finish/recover 的时刻），不是预留时刻。
4. reserve() 会写入 in_flight（pending）标记，**同一 identity 同时只允许
   一个未完成**的提交（原子预留）；另一个 identity 的 pending 不会阻止本
   身份。进程崩溃留下的 pending **不会**被自动重试，只能由调用方显式
   调用 recover_pending() 把它标记为 unknown，并从当前时刻再等 300 秒
   （只影响对应 identity）。
5. 所有写操作在 BEGIN IMMEDIATE 事务里完成，跨进程原子；每个操作独立
   开连接、操作完即关闭，不留锁。
6. 状态全部落盘（WAL + synchronous=FULL，断电也不丢已预留的计次），重启
   后保留；系统时钟回拨不会提前放行（内部维护"见过的最大 now"保护结束
   时间，检查冷却仍用实际 now）。该护栏只用于时钟安全，不产生跨账号冷却。
7. global_state 仅保留为**统计信息**（最后一次提交时间等），不参与任何
   reserve/status/summary 的资格与冷却判定，旧数据里的未来时间也不会
   阻塞新账号。
8. 库不提供任何修改 300 的参数；只接受持久化文件，拒绝 ":memory:"。
   MAX_PER_IDENTITY=10 仅作为旧导出/单轮默认的兼容常量保留，**不参与任何
   资格判定**；status/summary 的 quota、remaining 为 None，exhausted 恒为
   False，blocked 只由冷却与 pending 决定。

典型用法::

    from submission_state import StateStore, hash_identity, LimitError

    store = StateStore("state.sqlite3")
    ident = hash_identity(openid)          # 只保存散列，不保存 openid
    store.sync_history(ident, remote_count, remote_last_at)   # 可选
    try:
        attempt_id = store.reserve(ident)  # 只受 300 秒冷却与 pending 限制
    except LimitError as exc:
        print(exc, exc.wait_seconds, exc.exhausted)
        raise
    try:
        ok = do_submit()                   # 真实提交（网络）在调用方
    except Exception:
        store.finish(attempt_id, "rejected")   # 失败同样计次数并冷却
        raise
    store.finish(attempt_id, "success" if ok else "unknown")

仅依赖 Python 3.10 标准库（sqlite3 / hashlib / math / os / time / uuid）。
"""

from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

__all__ = [
    "MAX_PER_IDENTITY",
    "MIN_INTERVAL_SECONDS",
    "OUTCOME_SUCCESS",
    "OUTCOME_REJECTED",
    "OUTCOME_UNKNOWN",
    "StateStoreError",
    "LimitError",
    "InvalidArgument",
    "AttemptError",
    "StateStore",
    "hash_identity",
]

# --------------------------------------------------------------------------
# 常量：库不提供修改间隔的入口；累计次数不再设限
# --------------------------------------------------------------------------
# 仅为兼容旧导出/单轮默认值保留，**不参与任何资格判定**。
# 历史或本地 used 达到/超过它都可以继续提交（需求：21 次也不再限制）。
MAX_PER_IDENTITY = 10
MIN_INTERVAL_SECONDS = 300.0       # 同一身份两次提交的最小间隔

OUTCOME_PENDING = "pending"        # 内部状态：已预留、未结束
OUTCOME_SUCCESS = "success"        # 远端确认成功
OUTCOME_REJECTED = "rejected"      # 远端明确拒绝/失败
OUTCOME_UNKNOWN = "unknown"        # 结果不确定（超时、崩溃、恢复出来的）

_FINISHED_OUTCOMES = (OUTCOME_SUCCESS, OUTCOME_REJECTED, OUTCOME_UNKNOWN)

_SCHEMA_VERSION = "1"


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------
class StateStoreError(Exception):
    """所有本库异常的基类。"""


class LimitError(StateStoreError):
    """冷却未到或存在未完成提交时由 reserve() 抛出。

    累计次数不再设限，因此本库自身**不会**产出 exhausted=True。

    属性：
        message      中文说明（str(e) 即此文本）
        wait_seconds 还需等待的秒数（向上取整），0 表示不是冷却原因
        exhausted    兼容字段，恒为 False（不再有累计额度限制）
        identity     相关身份散列
        reason       'cooldown' | 'in_flight'
        pending      reason=='in_flight' 时为 True：该 identity 存在待处理
                     提交，此时等待（睡眠）没有意义，需要 finish()/
                     recover_pending()（另一个身份的 pending 不影响本身份）
    """

    def __init__(
        self,
        message: str,
        wait_seconds: float = 0.0,
        exhausted: bool = False,
        identity: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.wait_seconds = float(wait_seconds)
        self.exhausted = bool(exhausted)
        self.identity = identity
        self.reason = reason
        self.pending = reason == "in_flight"

    @property
    def wait_seconds(self) -> float:
        """还需等待的秒数（向上取整）。in_flight 时无意义，恒为 0。"""
        if self.pending:
            return 0.0
        return self._wait_seconds

    @wait_seconds.setter
    def wait_seconds(self, value: float) -> None:
        self._wait_seconds = float(value)


class InvalidArgument(StateStoreError, ValueError):
    """入参非法（identity / count / last_at / now / outcome 等）。"""


class AttemptError(StateStoreError, ValueError):
    """attempt_id 不存在，或该 attempt 已经结束过。"""


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def hash_identity(raw: str) -> str:
    """把 openid 之类的原始标识散列为可落库的 identity。

    库里只保存这个散列值，不会保存 openid 原文。调用方必须保证对同一个
    微信身份始终传入同一个原始值（别名/昵称不同不影响散列结果）。
    """
    if not isinstance(raw, str):
        raise InvalidArgument("hash_identity() 只接受 str，不要传入 openid 以外的对象")
    if not raw:
        raise InvalidArgument("hash_identity() 的入参不能为空")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _coerce_identity(identity: Any) -> str:
    if not isinstance(identity, str):
        raise InvalidArgument("identity 必须是 str（调用方传入的散列值），不要传 openid 原文")
    if not identity.strip():
        raise InvalidArgument("identity 不能为空字符串")
    return identity


def _coerce_now(now: Any, name: str = "now") -> float:
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        raise InvalidArgument("%s 必须是有限的数字（Unix 秒），收到 %r" % (name, now))
    value = float(now)
    if not math.isfinite(value):
        raise InvalidArgument("%s 必须是有限的数字（Unix 秒），不能是 nan/inf")
    return value


def _coerce_count(count: Any) -> int:
    if isinstance(count, bool) or not isinstance(count, int):
        raise InvalidArgument("count 必须是非负整数，收到 %r" % (count,))
    if count < 0:
        raise InvalidArgument("count 不能为负数，收到 %d" % count)
    return int(count)


def _coerce_last_at(last_at: Any) -> Optional[float]:
    if last_at is None:
        return None
    return _coerce_now(last_at, "last_at")


def _ceil_wait(deadline: Optional[float], now: float) -> float:
    if deadline is None:
        return 0.0
    remaining = deadline - now
    if remaining <= 0:
        return 0.0
    return float(math.ceil(remaining))


# --------------------------------------------------------------------------
# 存储
# --------------------------------------------------------------------------
class StateStore:
    """基于 SQLite 的提交状态存储。

    每个公开方法都会自己开连接、用完即关；写操作在 BEGIN IMMEDIATE 里完成，
    因此多个进程/线程共用同一个 db 文件是安全的。

    只接受**持久化文件路径**：每操作一条新连接，":memory:" 会在关闭时丢
    数据，所以构造时就拒绝（见 __init__）。
    """

    def __init__(self, path: str, busy_timeout: float = 30.0) -> None:
        """
        path          持久化 SQLite 文件路径；拒绝空路径与 ":memory:"
                      （每操作新连接，内存库无法跨连接保留状态）。
        busy_timeout  等锁的秒数，仅用于并发等待，不改变任何业务规则。
        """
        if not isinstance(path, (str, os.PathLike)):
            raise InvalidArgument("path 必须是数据库文件路径（str/PathLike），不支持 :memory:")
        self.path = os.fspath(path)
        if not self.path.strip() or self.path == ":memory:" or "mode=memory" in self.path:
            raise InvalidArgument("状态库必须使用持久化文件；每操作新连接，不支持 :memory: 内存库")
        self._busy_timeout = _coerce_now(busy_timeout, "busy_timeout")
        if self._busy_timeout <= 0:
            raise InvalidArgument("busy_timeout 必须为正数")
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    # -- 只读属性 ---------------------------------------------------------
    @property
    def max_per_identity(self) -> Optional[int]:
        """累计次数上限（只读）。

        已移除累计限制：恒为 None。used 仍会累加，仅供日志展示与 pending
        自动核对；reserve() 绝不因累计次数拒绝。
        """
        return None

    @property
    def min_interval(self) -> float:
        """两次提交的最小间隔秒数（只读）。"""
        return MIN_INTERVAL_SECONDS

    # -- 可选：上下文管理器（不是必须） -----------------------------------
    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def close(self) -> None:
        """没有常驻连接，空实现，仅为接口完整性保留。"""
        return None

    # -- 连接/事务 --------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        """每个操作一条短连接；PRAGMA 失败直接抛出（不吞异常）。"""
        conn = sqlite3.connect(self.path, timeout=self._busy_timeout, isolation_level=None)
        try:
            # 断电也不能丢掉已预留的计次：同步写盘 FULL + WAL
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA busy_timeout = %d" % int(self._busy_timeout * 1000))
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta ("
            " key TEXT PRIMARY KEY,"
            " value TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS identities ("
            " identity TEXT PRIMARY KEY,"
            " used INTEGER NOT NULL DEFAULT 0,"
            " last_at REAL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS attempts ("
            " attempt_id TEXT PRIMARY KEY,"
            " identity TEXT NOT NULL,"
            " reserved_at REAL NOT NULL,"
            " finished_at REAL,"
            " outcome TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS global_state ("
            " id INTEGER PRIMARY KEY CHECK (id = 1),"
            " last_at REAL)"
        )
        conn.execute("INSERT OR IGNORE INTO global_state(id, last_at) VALUES(1, NULL)")
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (_SCHEMA_VERSION,),
        )
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if version is None or version[0] != _SCHEMA_VERSION:
            raise sqlite3.DatabaseError("状态库版本不匹配，拒绝继续")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_attempts_identity ON attempts(identity)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_attempts_outcome ON attempts(outcome)"
        )

    def _stored_now_guard(self, conn: sqlite3.Connection) -> Optional[float]:
        row = conn.execute("SELECT value FROM meta WHERE key='last_seen_now'").fetchone()
        if row is None:
            return None
        try:
            value = float(row[0])
            if not math.isfinite(value):
                raise ValueError
            return value
        except (TypeError, ValueError):
            raise sqlite3.DatabaseError("状态库时间护栏已损坏，拒绝继续") from None

    def _effective_now(self, conn: sqlite3.Connection, now: Any, write: bool, *, completion: bool = False) -> float:
        """冷却检查使用实际时钟；结束时间使用保守的单调护栏。

        不能把历史最大时间当成当前时间解除冷却，否则回拨后可能提前放行。
        now 为 None 时取真实时钟 time.time()。
        """
        value = time.time() if now is None else _coerce_now(now)
        if not math.isfinite(value):
            raise InvalidArgument("now 必须是有限的数字（Unix 秒），不能是 nan/inf")
        guard = self._stored_now_guard(conn)
        if guard is not None and guard > value:
            return guard if completion else value
        if write and (guard is None or value > guard):
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('last_seen_now', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (repr(value),),
            )
        return value

    @staticmethod
    def _identity_row(
        conn: sqlite3.Connection, identity: str, create: bool = False
    ) -> Optional[tuple]:
        row = conn.execute(
            "SELECT used, last_at FROM identities WHERE identity=?", (identity,)
        ).fetchone()
        if row is None and create:
            conn.execute(
                "INSERT INTO identities(identity, used, last_at) VALUES(?, 0, NULL)",
                (identity,),
            )
            row = (0, None)
        return row

    @staticmethod
    def _pending_row(conn: sqlite3.Connection, identity: Optional[str] = None) -> Optional[tuple]:
        """该 identity 的未完成尝试；identity 为 None 时统计全部（仅供展示）。"""
        if identity is None:
            return conn.execute(
                "SELECT attempt_id, identity FROM attempts WHERE outcome=? ORDER BY reserved_at LIMIT 1",
                (OUTCOME_PENDING,),
            ).fetchone()
        return conn.execute(
            "SELECT attempt_id, identity FROM attempts WHERE outcome=? AND identity=? ORDER BY reserved_at LIMIT 1",
            (OUTCOME_PENDING, identity),
        ).fetchone()

    @staticmethod
    def _identity_deadline(identity_last: Optional[float]) -> Optional[float]:
        """冷却期限只由该身份自己的最后提交时间决定；全局统计不参与。"""
        if identity_last is None:
            return None
        return float(identity_last) + MIN_INTERVAL_SECONDS

    @staticmethod
    def _global_last(conn: sqlite3.Connection) -> Optional[float]:
        """仅统计信息：任何资格判定都不得使用（不产生跨账号冷却）。"""
        row = conn.execute("SELECT last_at FROM global_state WHERE id=1").fetchone()
        if row is None:
            conn.execute("INSERT OR IGNORE INTO global_state(id, last_at) VALUES(1, NULL)")
            return None
        return row[0]

    # -- 远端历史同步 ------------------------------------------------------
    def sync_history(self, identity: str, count: int, last_at: Optional[float] = None) -> Dict[str, Any]:
        """同步远端累积总次数与最后提交时间。

        used 取 max(本地, 远端)，last_at 取 max(本地, 远端)；只会单调增长，
        不会因为远端数据缺失而回退。**不会**清除或影响 pending 的 attempt。

        远端时间同时写入 global_state 作为**统计信息**（最后一次远端提交
        时间）；按需求它**不会**给其他账号制造冷却，切换 openid 可以立即
        提交。
        """
        identity = _coerce_identity(identity)
        count = _coerce_count(count)
        last_at = _coerce_last_at(last_at)
        conn = self._connect()
        committed = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            conn.execute(
                "INSERT INTO identities(identity, used, last_at) VALUES(?, ?, ?)"
                " ON CONFLICT(identity) DO UPDATE SET"
                "  used = MAX(identities.used, excluded.used),"
                "  last_at = CASE"
                "    WHEN excluded.last_at IS NULL THEN identities.last_at"
                "    WHEN identities.last_at IS NULL THEN excluded.last_at"
                "    ELSE MAX(identities.last_at, excluded.last_at) END",
                (identity, count, last_at),
            )
            if last_at is not None:
                # 仅统计：不参与资格判定，不给其他身份制造冷却。
                conn.execute(
                    "INSERT INTO global_state(id, last_at) VALUES(1, ?)"
                    " ON CONFLICT(id) DO UPDATE SET last_at = CASE"
                    "  WHEN global_state.last_at IS NULL THEN excluded.last_at"
                    "  ELSE MAX(global_state.last_at, excluded.last_at) END",
                    (last_at,),
                )
            row = self._identity_row(conn, identity)
            conn.execute("COMMIT")
            committed = True
            return {
                "identity": identity,
                "used": int(row[0]),
                "last_at": row[1],
            }
        finally:
            if not committed:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            conn.close()

    # -- 查询 --------------------------------------------------------------
    def status(self, identity: str, now: Optional[float] = None) -> Dict[str, Any]:
        """返回该身份当前状态。

        必含字段：used / remaining / wait_seconds / blocked。
        额外字段：exhausted / pending / in_flight / pending_attempt_id /
        last_at / global_last_at / global_wait_seconds / quota / interval /
        now。
        - blocked：本身份不能提交（冷却未到或存在 pending）。**累计次数不再
          参与判定**：used 再大也不会让 blocked 为真；另一个身份的提交或
          pending 不影响本身份。
        - remaining / quota：恒为 None（已无累计额度），仅为接口兼容保留。
        - exhausted：恒为 False，仅为接口兼容保留。
        - pending：仅该 identity 存在待处理提交；True 时不应睡眠等待，而应
          finish() 或 recover_pending()。
        - in_flight：与 pending 同义的别名。
        - global_last_at / global_wait_seconds：仅统计展示，不参与 blocked。
        """
        identity = _coerce_identity(identity)
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            now_eff = self._effective_now(conn, now, write=False)
            row = self._identity_row(conn, identity)
            used = int(row[0]) if row else 0
            identity_last = row[1] if row else None
            global_last = self._global_last(conn)
            pending = self._pending_row(conn, identity)
            wait = _ceil_wait(self._identity_deadline(identity_last), now_eff)
            return {
                "identity": identity,
                "used": used,
                "remaining": None,
                "wait_seconds": wait,
                "blocked": bool(wait > 0 or pending is not None),
                "exhausted": False,
                "pending": pending is not None,
                "in_flight": pending is not None,
                "pending_attempt_id": pending[0] if pending else None,
                "pending_identity": pending[1] if pending else None,
                "last_at": identity_last,
                "global_last_at": global_last,
                "global_wait_seconds": _ceil_wait(
                    None if global_last is None else float(global_last) + MIN_INTERVAL_SECONDS,
                    now_eff,
                ),
                "quota": None,
                "interval": MIN_INTERVAL_SECONDS,
                "now": now_eff,
            }
        finally:
            conn.close()

    def summary(self, now: Optional[float] = None) -> Dict[str, Any]:
        """全部身份的汇总，便于 CLI 展示（每个身份独立判定，互不影响）。"""
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            now_eff = self._effective_now(conn, now, write=False)
            global_last = self._global_last(conn)
            pending_rows = conn.execute(
                "SELECT attempt_id, identity, reserved_at FROM attempts WHERE outcome=? ORDER BY reserved_at",
                (OUTCOME_PENDING,),
            ).fetchall()
            pending_by_identity = {r[1]: r[0] for r in pending_rows}
            items: Dict[str, Dict[str, Any]] = {}
            total_used = 0
            for identity, used, last_at in conn.execute(
                "SELECT identity, used, last_at FROM identities ORDER BY identity"
            ).fetchall():
                used = int(used)
                wait = _ceil_wait(self._identity_deadline(last_at), now_eff)
                own_pending = pending_by_identity.get(identity)
                items[identity] = {
                    "used": used,
                    "remaining": None,
                    "wait_seconds": wait,
                    # 只由冷却与 pending 决定；累计次数不参与。
                    "blocked": bool(wait > 0 or own_pending is not None),
                    "exhausted": False,
                    "pending": own_pending is not None,
                    "pending_attempt_id": own_pending,
                    "last_at": last_at,
                }
                total_used += used
            return {
                "now": now_eff,
                "quota": None,
                "interval": MIN_INTERVAL_SECONDS,
                "identity_count": len(items),
                "total_used": total_used,
                "total_remaining": None,
                "global_last_at": global_last,
                "global_wait_seconds": _ceil_wait(
                    None if global_last is None else float(global_last) + MIN_INTERVAL_SECONDS,
                    now_eff,
                ),
                "in_flight": len(pending_rows) > 0,
                "pending": [
                    {
                        "attempt_id": r[0],
                        "identity": r[1],
                        "reserved_at": r[2],
                        "age_seconds": max(0.0, now_eff - float(r[2])),
                    }
                    for r in pending_rows
                ],
                "identities": items,
            }
        finally:
            conn.close()

    def pending(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """列出未完成的 attempt（进程崩溃遗留的 in_flight）。"""
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            now_value = self._effective_now(conn, now, write=False)
            rows = conn.execute(
                "SELECT attempt_id, identity, reserved_at FROM attempts WHERE outcome=? ORDER BY reserved_at",
                (OUTCOME_PENDING,),
            ).fetchall()
            return [
                {
                    "attempt_id": r[0],
                    "identity": r[1],
                    "reserved_at": r[2],
                    "age_seconds": max(0.0, now_value - float(r[2])),
                }
                for r in rows
            ]
        finally:
            conn.close()

    # -- 预留 / 结束 / 恢复 -------------------------------------------------
    def reserve(self, identity: str, now: Optional[float] = None) -> str:
        """预留一次提交，返回 attempt_id。

        used 在此时累加（失败/不确定也不退款），但**累计次数不再限制资格**：
        reserve() 绝不因 used 达到/超过 10、21 或 100 而拒绝。真正会拒绝的
        只有两类：本 identity 冷却未到、本 identity 存在 pending。
        判定只针对本 identity：其他账号的提交/pending 不阻止本次预留。
        """
        identity = _coerce_identity(identity)
        conn = self._connect()
        committed = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            now_eff = self._effective_now(conn, now, write=True)
            row = self._identity_row(conn, identity, create=True)
            used = int(row[0])
            identity_last = row[1]
            wait = _ceil_wait(self._identity_deadline(identity_last), now_eff)

            pending = self._pending_row(conn, identity)
            if pending is not None:
                raise LimitError(
                    "该身份存在未完成的提交（attempt_id=%s），不允许自动重试；"
                    "请先调用 finish() 或 recover_pending()" % pending[0],
                    wait_seconds=0.0,
                    exhausted=False,
                    identity=identity,
                    reason="in_flight",
                )
            if wait > 0:
                raise LimitError(
                    "该身份距上次提交不足 %d 秒，还需等待 %.0f 秒" % (int(MIN_INTERVAL_SECONDS), wait),
                    wait_seconds=wait,
                    exhausted=False,
                    identity=identity,
                    reason="cooldown",
                )
            attempt_id = uuid.uuid4().hex
            conn.execute(
                "UPDATE identities SET used=? WHERE identity=?", (used + 1, identity)
            )
            conn.execute(
                "INSERT INTO attempts(attempt_id, identity, reserved_at, finished_at, outcome)"
                " VALUES(?, ?, ?, NULL, ?)",
                (attempt_id, identity, now_eff, OUTCOME_PENDING),
            )
            conn.execute("COMMIT")
            committed = True
            return attempt_id
        finally:
            if not committed:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            conn.close()

    def finish(self, attempt_id: str, outcome: str, now: Optional[float] = None) -> Dict[str, Any]:
        """结束一次尝试。

        outcome 必须是 success / rejected / unknown 之一；三种情况都会计次数
        并从"完成时间"开始冷却（保守，不退款、不自动重试）。冷却只作用于
        该 attempt 所属身份。
        """
        if not isinstance(attempt_id, str) or not attempt_id.strip():
            raise InvalidArgument("attempt_id 必须是非空字符串")
        if not isinstance(outcome, str) or outcome not in _FINISHED_OUTCOMES:
            raise InvalidArgument(
                "outcome 必须是 %s 之一，收到 %r" % (" / ".join(_FINISHED_OUTCOMES), outcome)
            )
        conn = self._connect()
        committed = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            now_eff = self._effective_now(conn, now, write=True, completion=True)
            row = conn.execute(
                "SELECT identity, outcome FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise AttemptError("未知的 attempt_id：%s" % attempt_id)
            identity = row[0]
            if row[1] != OUTCOME_PENDING:
                raise AttemptError(
                    "attempt_id %s 已以 %s 结束，不能重复 finish" % (attempt_id, row[1])
                )
            conn.execute(
                "UPDATE attempts SET outcome=?, finished_at=? WHERE attempt_id=?",
                (outcome, now_eff, attempt_id),
            )
            self._identity_row(conn, identity, create=True)
            conn.execute(
                "UPDATE identities SET last_at = CASE WHEN last_at IS NULL THEN ?"
                " ELSE MAX(last_at, ?) END WHERE identity=?",
                (now_eff, now_eff, identity),
            )
            conn.execute(
                "UPDATE global_state SET last_at = CASE WHEN last_at IS NULL THEN ?"
                " ELSE MAX(last_at, ?) END WHERE id=1",
                (now_eff, now_eff),
            )
            conn.execute("COMMIT")
            committed = True
            return {
                "attempt_id": attempt_id,
                "identity": identity,
                "outcome": outcome,
                "finished_at": now_eff,
            }
        finally:
            if not committed:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            conn.close()

    def recover_pending(self, now: Optional[float] = None) -> List[str]:
        """把崩溃遗留的 pending 标记为 unknown，并从当前时刻再等 300 秒。

        返回被恢复的 attempt_id 列表（按预留时间排序）。恢复后各身份的 used
        已被占用（不退款），冷却从 now 重新计时，且只影响对应身份；其他
        身份不受影响（累计次数不限制之后的提交）。
        """
        conn = self._connect()
        committed = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_schema(conn)
            now_eff = self._effective_now(conn, now, write=True, completion=True)
            rows = conn.execute(
                "SELECT attempt_id, identity FROM attempts WHERE outcome=? ORDER BY reserved_at",
                (OUTCOME_PENDING,),
            ).fetchall()
            recovered: List[str] = []
            for attempt_id, identity in rows:
                conn.execute(
                    "UPDATE attempts SET outcome=?, finished_at=? WHERE attempt_id=?",
                    (OUTCOME_UNKNOWN, now_eff, attempt_id),
                )
                self._identity_row(conn, identity, create=True)
                conn.execute(
                    "UPDATE identities SET last_at = CASE WHEN last_at IS NULL THEN ?"
                    " ELSE MAX(last_at, ?) END WHERE identity=?",
                    (now_eff, now_eff, identity),
                )
                conn.execute(
                    "UPDATE global_state SET last_at = CASE WHEN last_at IS NULL THEN ?"
                    " ELSE MAX(last_at, ?) END WHERE id=1",
                    (now_eff, now_eff),
                )
                recovered.append(attempt_id)
            conn.execute("COMMIT")
            committed = True
            return recovered
        finally:
            if not committed:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            conn.close()

"""Observed mini-program API flow; no fabricated identity or credential refresh."""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from capture_parser import APP_ID, BASE_URL, GATE_PATH, HISTORY_PATH, LOGIN_PATH, QUESTIONS_PATH, SUBMIT_PATH


class ApiError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never leak openid or login code to a redirect target; never replay POST.
        return None


class ApiClient:
    def __init__(self, timeout: float = 30):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("超时必须为正数")
        self.timeout = timeout
        self.opener = build_opener(NoRedirect())

    def request(self, method: str, path: str, *, query=None, body=None) -> dict:
        if path not in (LOGIN_PATH, GATE_PATH, HISTORY_PATH, QUESTIONS_PATH, SUBMIT_PATH):
            raise ApiError("禁止访问未识别的接口")
        url = BASE_URL + path
        if query is not None:
            url += "?" + urlencode(query)
        data = None if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Referer": f"https://servicewechat.com/{APP_ID}/27/page-frame.html",
            "xweb_xhr": "1",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) UnifiedPCWindowsWechat(0xf2541d41) XWEB/25560",
        }
        request = Request(url, data=data, headers=headers, method=method)
        try:
            # TLS certificate verification is enabled by urllib's default context.
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise ApiError(f"接口 {path} 未返回 HTTP 200")
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ApiError("接口响应超过安全大小限制")
                result = json.loads(raw.decode("utf-8-sig"))
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise ApiError(f"接口 {path} 返回 HTTP {code}；停止，不自动重试") from None
        except (URLError, OSError, http.client.HTTPException, UnicodeError, ValueError):
            # Exception strings may contain the entire credential-bearing request URL.
            raise ApiError(f"接口 {path} 连接失败或响应无法解析；停止，不自动重试") from None
        if not isinstance(result, dict):
            raise ApiError("接口返回的不是 JSON 对象")
        return result

    def login(self, code: str) -> str:
        if not isinstance(code, str) or not code.strip() or any(c.isspace() for c in code):
            raise ApiError("请输入当前账号刚通过 wx.login 获得且尚未使用的 code")
        result = self.request("POST", LOGIN_PATH, query={"code": code}, body={})
        data = result.get("data")
        if result.get("status") != 200 or not isinstance(data, dict):
            raise ApiError("登录失败；旧 code 不可重放，请在当前微信账号重新登录")
        openid = data.get("openid")
        validate_openid(openid)
        # session_key is deliberately not returned, logged or persisted.
        return openid

    def history(self, openid: str) -> tuple[int, float | None]:
        result = self.request("GET", HISTORY_PATH, query={"userId": openid})
        if result.get("status") != 200 or result.get("message") != "查询成功" or not isinstance(result.get("data"), list):
            raise ApiError("无法确认历史次数，停止提交；请核对账号授权")
        records = {}
        for record in result["data"]:
            if not isinstance(record, dict) or not isinstance(record.get("treUserWjId"), str) or not record["treUserWjId"]:
                raise ApiError("历史记录格式发生变化，无法安全计算次数")
            try:
                stamp = datetime.strptime(record["createDate"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=8))).timestamp()
            except (KeyError, TypeError, ValueError):
                raise ApiError("历史时间格式发生变化，无法安全计算间隔") from None
            key = record["treUserWjId"]
            if key in records and records[key] != stamp:
                raise ApiError("同一历史记录 ID 出现不一致时间，无法确认次数，停止")
            records[key] = stamp
        # The observed history endpoint has no survey ID: conservatively count all records.
        # Timestamp has only second precision; add one second to avoid rounding early.
        return len(records), max(records.values()) + 1 if records else None

    def available_questions(self, openid: str, survey_id: str) -> list[dict]:
        query = {"treOfWjId": survey_id, "userId": openid}
        gate = self.request("GET", GATE_PATH, query={**query, "type": ""})
        if gate.get("status") != 200 or gate.get("message") != "可填写" or gate.get("data") != "N":
            raise ApiError("服务端未明确允许填写（可能已满额、未到间隔或授权失效），停止")
        questions = self.request("GET", QUESTIONS_PATH, query=query)
        if questions.get("status") != 200 or questions.get("message") != "可填写" or not isinstance(questions.get("data"), list):
            raise ApiError("服务端未返回可填写问卷，停止")
        return questions["data"]

    def submit(self, payload: dict) -> dict:
        return self.request("POST", SUBMIT_PATH, body=payload)


def validate_openid(openid: object) -> None:
    if not isinstance(openid, str) or not openid.strip() or len(openid) > 128 or any(c.isspace() for c in openid):
        raise ApiError("openid 为空或格式无效；必须使用本人有权使用账号的真实登录结果")


def identity_key(openid: str) -> str:
    validate_openid(openid)
    return hashlib.sha256(f"{BASE_URL}|{APP_ID}|{openid}".encode("utf-8")).hexdigest()


class AccountStore:
    """Local plaintext identity store excluded from Git; stable quota key ignores aliases."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS accounts (alias TEXT PRIMARY KEY, openid TEXT NOT NULL)")

    def save(self, alias: str, openid: str) -> None:
        validate_openid(openid)
        if not alias or len(alias) > 80 or any(ord(c) < 32 for c in alias):
            raise ApiError("账号别名无效")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO accounts(alias,openid) VALUES(?,?) ON CONFLICT(alias) DO UPDATE SET openid=excluded.openid", (alias, openid))

    def get(self, alias: str) -> str:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT openid FROM accounts WHERE alias=?", (alias,)).fetchone()
        if row is None:
            raise ApiError("未找到账号；先 import-account 或 login-code")
        return row[0]

    def list_accounts(self) -> list[tuple[str, str]]:
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT alias,openid FROM accounts ORDER BY alias").fetchall()
        return [(alias, identity_key(openid)[:12]) for alias, openid in rows]

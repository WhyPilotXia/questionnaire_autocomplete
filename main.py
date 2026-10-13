"""问卷自动填写：默认最多十次、每次间隔301秒；preview 或 --dry-run 仅预览。"""
from __future__ import annotations

import argparse
import getpass
import json
import math
import runpy
import sqlite3
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta

from capture_parser import import_openid
from questionnaire import Questionnaire, load_names, preview
from submission_state import LimitError, StateStore
from wechat_client import AccountStore, ApiClient, ApiError, identity_key, validate_openid

ROOT = Path(__file__).resolve().parent
LOCAL = ROOT / ".local"
ACCOUNTS_DB = LOCAL / "accounts.sqlite3"
STATE_DB = LOCAL / "submissions.sqlite3"
SETTINGS_FILE = ROOT / "local_settings.py"
DEFAULT_COUNT = 10
SUBMISSION_INTERVAL_SECONDS = 301


def log(message: str, level: str = "INFO") -> None:
    """Flush timestamped messages immediately, without credentials or raw responses."""
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
    print(f"[{stamp}] [{level}] {message}", file=sys.stderr if level == "ERROR" else sys.stdout, flush=True)


def wait_before_next(seconds: int) -> None:
    next_at = (datetime.now().astimezone() + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S%z")
    log(f"等待 {seconds} 秒；下次检查时间 {next_at}（Ctrl+C 可退出）。")
    # monotonic prevents an early wake-up from shortening the requested interval.
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(remaining)


def default_openid() -> str:
    """Read each run, so changing the config never leaves a cached identity."""
    if not SETTINGS_FILE.is_file():
        raise ApiError("请复制 local_settings.example.py 为 local_settings.py 并填写 OPENID，或传 --openid")
    config = runpy.run_path(str(SETTINGS_FILE))
    openid = config.get("OPENID")
    validate_openid(openid)
    return openid


def resolve_openid(args) -> str:
    if args.openid is not None:
        openid = args.openid
    elif args.account is not None:
        openid = AccountStore(ACCOUNTS_DB).get(args.account)
    else:
        openid = default_openid()
    validate_openid(openid)
    return openid


def add_run_options(parser, *, child=False):
    # SUPPRESS on child parsers preserves options written before a subcommand.
    default = argparse.SUPPRESS if child else None
    identity = parser.add_mutually_exclusive_group()
    identity.add_argument("--openid", default=default, help="当前微信 openid；省略时读取 local_settings.py 的 OPENID")
    identity.add_argument("--account", default=default, help="可选：使用先前导入的账号别名")
    parser.add_argument("--count", type=int, choices=range(1, 11), default=argparse.SUPPRESS if child else DEFAULT_COUNT, help="本轮最多提交次数，默认10次（不超过账号剩余额度）")
    parser.add_argument("--wait", action=argparse.BooleanOptionalAction, default=argparse.SUPPRESS if child else True, help="默认自动等待301秒间隔；--no-wait 遇冷却立即退出")
    parser.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS if child else False, help="仅离线预览，不联网、不计次")


def submit_once(questionnaire: Questionnaire, names: list[str], openid: str,
                client: ApiClient, state: StateStore) -> dict:
    key = identity_key(openid)
    # A fresh history check includes submissions made outside this program.
    count, last_at = client.history(openid)
    state.sync_history(key, count, last_at)
    current = state.status(key)
    if current.get("in_flight"):
        pending = [item for item in state.pending() if item["identity"] == key]
        # A previous process may have died after the server accepted the request
        # but before finish(). Reconcile only with strong evidence: remote count
        # covers the local reservation and the remote record is not older than it.
        if (len(pending) == 1 and count >= current["used"] and last_at is not None
                and last_at >= pending[0]["reserved_at"]):
            state.finish(pending[0]["attempt_id"], "success", now=last_at)
            log(f"检测到上次进程遗留记录 {pending[0]['attempt_id']} 已出现在服务端历史中；自动确认成功，不重复提交。")
            current = state.status(key)
    log(f"账号历史 {count} 次；本地保守计次 {current['used']}/10，剩余 {current['remaining']} 次。")
    last_at = current.get("last_at")
    if last_at is not None:
        current["wait_seconds"] = max(current["wait_seconds"], math.ceil(max(0, last_at + SUBMISSION_INTERVAL_SECONDS - time.time())))
        current["blocked"] = current["blocked"] or current["wait_seconds"] > 0
    if current["blocked"]:
        message = "本地额度或301秒间隔阻止本次操作"
        if current.get("in_flight"):
            message = "存在未结束提交；请先确认原进程停止并核实结果，再执行 recover-pending --confirm-stopped"
        reason = "in_flight" if current.get("in_flight") else ("exhausted" if current["remaining"] == 0 else "cooldown")
        raise LimitError(message, wait_seconds=current["wait_seconds"], exhausted=current["remaining"] == 0, identity=key, reason=reason)
    schema = client.available_questions(openid, questionnaire.survey_id)
    questionnaire.check_live_schema(schema)
    payload = questionnaire.build(openid, names)
    attempt = state.reserve(key)
    outcome = "unknown"
    try:
        response = client.submit(payload)
        if response.get("status") == 200 and response.get("message") == "提交成功":
            outcome = "success"
            return {"attempt": attempt, "result": "提交成功"}
        # HTTP 200 alone is NOT success; unrecognized replies remain ambiguous.
        if isinstance(response.get("status"), int) and response["status"] != 200:
            outcome = "rejected"
        raise ApiError("服务端未明确返回提交成功；已保守计次并停止，请在小程序核实")
    finally:
        # Includes network errors, Ctrl+C and JSON/business failures. No retry/refund.
        # Hard termination leaves a pending row, blocking further submissions.
        state.finish(attempt, outcome)


def run_submissions(questionnaire, names, openid, client, state, *, count, wait):
    if not 1 <= count <= 10:
        raise ValueError("本次提交数量必须为 1 到 10")
    key = identity_key(openid)
    completed = 0
    while completed < count:
        log(f"开始第 {completed + 1}/{count} 次提交检查。")
        try:
            result = submit_once(questionnaire, names, openid, client, state)
        except LimitError as exc:
            status = state.status(key)
            if exc.exhausted:
                log(f"当前账号十次总额度已用完；本轮成功 {completed} 次，停止。")
                return completed
            if not wait or not status["wait_seconds"] or status.get("in_flight", False):
                log(f"提交受限：{exc}；本轮成功 {completed} 次。", "ERROR")
                raise
            seconds = max(1, math.ceil(status["wait_seconds"]))
            log(f"当前账号仍在301秒间隔内，需等待 {seconds} 秒。")
            wait_before_next(seconds)
            continue
        except Exception as exc:
            log(f"第 {completed + 1}/{count} 次发生异常：{exc}；为避免重复提交，本轮停止。", "ERROR")
            raise

        completed += 1
        log(f"第 {completed}/{count} 次提交成功；记录 {result['attempt']}。")
        if completed >= count:
            break
        status = state.status(key)
        if status["remaining"] == 0:
            log(f"当前账号十次总额度已用完；本轮成功 {completed} 次，停止。")
            break
        if wait:
            # User-facing interval is deliberately 301 seconds, one second above
            # the server/local minimum. Count from the completed request.
            wait_before_next(SUBMISSION_INTERVAL_SECONDS)
        else:
            log(f"已启用 --no-wait；本轮成功 {completed} 次后停止。")
            break
    log(f"本轮结束：成功 {completed} 次，计划上限 {count} 次。")
    return completed


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, default=ROOT / "capture" / "1_Full.txt", help="完整问卷模板抓包")
    parser.add_argument("--names", type=Path, default=ROOT / "names.json", help="纯姓名 JSON 列表")
    add_run_options(parser)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("preview", help="离线生成答案摘要，不联网、不计次")
    importer = sub.add_parser("import-account", help="从当前微信账号登录响应中自动提取 openid，不联网")
    importer.add_argument("--account", required=True, help="本地账号别名")
    importer.add_argument("--from-capture", type=Path, required=True)
    login = sub.add_parser("login-code", help="使用当前账号新鲜、未消费的 wx.login code 登录（联网）")
    login.add_argument("--account", required=True)
    login.add_argument("--code-file", type=Path, help="本地 code 文件；省略则隐藏输入，不进入命令历史")
    sub.add_parser("accounts", help="列出本地账号别名和脱敏指纹")
    status = sub.add_parser("status", help="查看本地状态；--refresh 联网同步该账号历史")
    add_run_options(status, child=True)
    status.add_argument("--refresh", action="store_true")
    submit = sub.add_parser("submit", help="真实提交当前账号；默认最多10次、间隔301秒")
    add_run_options(submit, child=True)
    submit.add_argument("--execute", action="store_true", help="兼容旧命令：现在提交默认执行，无需此参数")
    recover = sub.add_parser("recover-pending", help="确认所有提交进程已停止后，将崩溃遗留状态标为不确定并重新冷却")
    recover.add_argument("--confirm-stopped", action="store_true", required=True)
    return parser


def main(argv=None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.command in (None, "submit", "status") and args.openid is not None and args.account is not None:
        parser.error("--openid 与 --account 不能同时指定")
    command = args.command or "submit"
    try:
        if command in ("preview", "submit"):
            questionnaire = Questionnaire.from_capture(args.capture)
            names = load_names(args.names)
            if command == "preview" or args.dry_run:
                payload = questionnaire.build("offline-preview-not-an-account", names)
                print(json.dumps(preview(payload), ensure_ascii=False, indent=2))
                return 0
        if command in ("import-account", "login-code", "accounts"):
            accounts = AccountStore(ACCOUNTS_DB)
        if command == "import-account":
            openid = import_openid(args.from_capture)
            accounts.save(args.account, openid)
            print(f"账号已保存，指纹 {identity_key(openid)[:12]}；未联网，未保存 session_key")
        elif command == "login-code":
            code = args.code_file.read_text(encoding="utf-8-sig").strip() if args.code_file else getpass.getpass("当前账号新鲜且未消费的 code: ")
            openid = ApiClient().login(code)
            accounts.save(args.account, openid)
            print(f"登录信息已保存，指纹 {identity_key(openid)[:12]}；未保存 code/session_key")
        elif command == "accounts":
            print(json.dumps(accounts.list_accounts(), ensure_ascii=False, indent=2))
        elif command == "recover-pending":
            recovered = StateStore(STATE_DB).recover_pending()
            if recovered:
                print("已将遗留提交记为不确定，保留次数并重新等待至少五分钟；请另在小程序核实结果。")
            else:
                print("没有待恢复的提交；次数不变。")
        else:
            openid = resolve_openid(args)
            state = StateStore(STATE_DB)
            if command == "status":
                if args.refresh:
                    count, last_at = ApiClient().history(openid)
                    state.sync_history(identity_key(openid), count, last_at)
                print(json.dumps(state.status(identity_key(openid)), ensure_ascii=False, indent=2))
            elif command == "submit":
                log(f"真实提交模式：账号指纹 {identity_key(openid)[:12]}，本轮最多 {args.count} 次；成功后间隔301秒，仅该账号受限。")
                run_submissions(questionnaire, names, openid, ApiClient(), state, count=args.count, wait=args.wait)
        return 0
    except LimitError as exc:
        suffix = ""
        if exc.exhausted:
            suffix = "；当前账号十次额度已用完"
        elif exc.pending:
            suffix = "；该状态不能靠等待自动解除"
        elif exc.wait_seconds:
            suffix = f"；仍需等待 {math.ceil(exc.wait_seconds)} 秒"
        log(f"已停止：{exc}{suffix}。", "ERROR")
        return 2
    except (ApiError, ValueError) as exc:
        # Our business exceptions never include raw request/response credentials.
        if isinstance(exc, json.JSONDecodeError):
            log("已停止：本地 JSON 文件格式错误。", "ERROR")
        else:
            log(f"已停止：{exc}", "ERROR")
        return 2
    except (SyntaxError, NameError):
        log("已停止：local_settings.py 配置语法错误；请使用 OPENID = \"你的openid\"。", "ERROR")
        return 2
    except (OSError, sqlite3.Error):
        log("已停止：本地文件或数据库不可访问/已损坏；不会清空状态或继续提交。", "ERROR")
        return 2
    except KeyboardInterrupt:
        log("已退出；已发起的提交保守保留计次，请核实服务端结果。", "ERROR")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

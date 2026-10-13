"""Questionnaire validation and random answer construction (no network I/O)."""
from __future__ import annotations

import copy
import json
import random
from pathlib import Path

from capture_parser import CaptureError, QUESTIONS_PATH, SUBMIT_PATH, find_exchange, read_exchanges


class TemplateError(ValueError):
    pass


def load_names(path: Path) -> list[str]:
    names = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n.strip() for n in names):
        raise TemplateError("姓名列表必须是非空 JSON 字符串数组；先运行 prepare_names.py")
    return sorted(set(n.strip() for n in names))


def validate_questions(questions: object, *, answered: bool) -> None:
    if not isinstance(questions, list) or not questions:
        raise TemplateError("问卷题目为空或格式不符")
    seen = set()
    for question in questions:
        if not isinstance(question, dict):
            raise TemplateError("题目不是对象")
        sid = question.get("subjectId")
        if not isinstance(sid, str) or not sid or sid in seen:
            raise TemplateError("题目 ID 缺失或重复")
        seen.add(sid)
        if question.get("subjectType") not in ("单选", "多选"):
            raise TemplateError("发现尚未支持的题型，请更新模板和实现")
        if question.get("showTitle") not in (True, "true") or question.get("subjectYl1") or question.get("cxType") not in (None, "0", 0):
            raise TemplateError("发现隐藏题或未支持的联动规则，停止随机填答")
        options = question.get("optionList")
        if not isinstance(options, list) or not options:
            raise TemplateError("题目没有选项")
        ids = []
        for option in options:
            if not isinstance(option, dict) or not isinstance(option.get("optionId"), str) or not option["optionId"]:
                raise TemplateError("选项 ID 格式不符")
            ids.append(option["optionId"])
            if option.get("subjectId", sid) != sid or option.get("delFlag", "0") != "0":
                raise TemplateError("选项归属不符或已删除")
            if option.get("ejOptionList") or option.get("ejOptionIds") or option.get("isbt", "N") != "N":
                raise TemplateError("发现二级选项或必填附加文本，停止提交")
            if option.get("optionIsok", "N") != "N" or option.get("optionIstrue", "") != "":
                raise TemplateError("选项状态编码与已分析抓包不同，请重新检查")
        if len(ids) != len(set(ids)):
            raise TemplateError("选项 ID 重复")
        if answered:
            selected = question.get("optionIds")
            if not isinstance(selected, str) or not selected:
                raise TemplateError("抓包中存在未填写题目")
            selected_ids = selected.split(",")
            if len(selected_ids) != len(set(selected_ids)) or not set(selected_ids).issubset(ids):
                raise TemplateError("抓包答案包含重复或未知选项")
            if question["subjectType"] == "单选" and len(selected_ids) != 1:
                raise TemplateError("单选答案不止一个")


def _normalized_schema(questions: list[dict]) -> list[dict]:
    result = copy.deepcopy(questions)
    for question in result:
        # These are the only two schema/request differences observed in the capture.
        question.pop("optionIds", None)
        question.pop("showTitle", None)
    return result


class Questionnaire:
    def __init__(self, payload: dict, schema: list[dict]):
        if not isinstance(payload, dict):
            raise TemplateError("提交模板不是对象")
        for key in ("treOfWjId", "wjInfoId", "officeId", "htCode", "userQy"):
            if not isinstance(payload.get(key), str) or not payload[key]:
                raise TemplateError(f"模板缺少 {key}")
        validate_questions(payload.get("data"), answered=True)
        validate_questions(schema, answered=False)
        if _normalized_schema(payload["data"]) != _normalized_schema(schema):
            raise TemplateError("抓包题目定义与提交模板不匹配")
        for question in payload["data"]:
            for key in ("treOfWjId", "wjInfoId", "officeId", "htCode"):
                if question.get(key) != payload[key]:
                    raise TemplateError("题目与问卷的标识不一致")
        self.payload = copy.deepcopy(payload)
        # A captured identity/name is never a fallback for a new submission.
        self.payload.pop("userId", None)
        self.payload.pop("userName", None)
        # The capture has empty phone/proof and literal location placeholders.
        # Refuse to replay real per-respondent data from a future capture.
        allowed = {"sjh": (None, ""), "pzurl": (None, ""),
                   "treUserWjYl1": (None, "", "经度"), "treUserWjYl2": (None, "", "纬度"),
                   "treUserWjYl3": (None, "", "地理位置")}
        if any(self.payload.get(key) not in values for key, values in allowed.items()):
            raise TemplateError("模板包含手机号、凭证或地理位置；需要显式适配，不能跨姓名/账号重放")
        self.schema = copy.deepcopy(schema)

    @classmethod
    def from_capture(cls, path: Path) -> "Questionnaire":
        exchanges = read_exchanges(path)
        submitted = find_exchange(exchanges, SUBMIT_PATH)
        schema_exchange = find_exchange(exchanges, QUESTIONS_PATH)
        if submitted.method != "POST" or schema_exchange.method != "GET":
            raise CaptureError("抓包接口请求方法与预期不符")
        for exchange in (submitted, schema_exchange):
            if exchange.http_status != 200 or not isinstance(exchange.response, dict) or exchange.response.get("status") != 200:
                raise CaptureError("模板必须来自成功获取并成功提交的抓包")
        if submitted.response.get("message") != "提交成功":
            raise CaptureError("未观察到明确的提交成功响应")
        return cls(submitted.request, schema_exchange.response.get("data"))

    @property
    def survey_id(self) -> str:
        return self.payload["treOfWjId"]

    def check_live_schema(self, schema: object) -> None:
        validate_questions(schema, answered=False)
        if _normalized_schema(schema) != _normalized_schema(self.schema):
            raise TemplateError("线上问卷已变化；请重新抓取完整问卷及成功提交模板，禁止盲目重放")

    def build(self, openid: str, names: list[str], rng=None) -> dict:
        if not isinstance(openid, str) or not openid.strip():
            raise TemplateError("必须显式提供当前微信账号的身份")
        if not names or any(not isinstance(n, str) or not n.strip() for n in names):
            raise TemplateError("姓名列表无效")
        rng = rng or random.SystemRandom()
        payload = copy.deepcopy(self.payload)
        payload["userId"] = openid
        payload["userName"] = rng.choice(names)
        for question in payload["data"]:
            if question["subjectType"] == "单选":
                question["optionIds"] = rng.choice(question["optionList"])["optionId"]
            # Multiple-choice objects, order and comma-separated selections remain exact.
        validate_questions(payload["data"], answered=True)
        return payload


def preview(payload: dict) -> dict:
    """Only display answer summary; never print credentials or raw profile metadata."""
    if not isinstance(payload, dict) or any(not isinstance(payload.get(k), str) or not payload[k] for k in ("userName", "treOfWjId")):
        raise TemplateError("预览缺少姓名或问卷标识")
    validate_questions(payload.get("data"), answered=True)
    return {
        "模式": "离线预览，不会提交",
        "姓名": payload["userName"],
        "问卷": payload["treOfWjId"],
        "答案": [
            {"题号": q.get("subjectNo"), "题型": q["subjectType"], "题目": q.get("subjectTitle"),
             "选择": [o["optionName"] for oid in q["optionIds"].split(",") for o in q["optionList"] if o["optionId"] == oid]}
            for q in payload["data"]
        ],
    }

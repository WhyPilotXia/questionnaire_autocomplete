"""Parse decrypted Fiddler Full text without replaying captured credentials."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

BASE_URL = "https://wjdc.xasxhl.com:8080"
APP_ID = "wxfacea951a0cbea7c"
LOGIN_PATH = "/captcha/wxlogin"
GATE_PATH = "/wjdcxcx/getSfjyByTreOfWjId"
QUESTIONS_PATH = "/wjdcxcx/pdByTreOfWjId"
SUBMIT_PATH = "/wjdcxcx/xzWjdcTreUserWjSubAndOpt"
HISTORY_PATH = "/wjdcxcx/queryTreUserWjByUserId"


class CaptureError(ValueError):
    pass


@dataclass
class Exchange:
    method: str
    url: str
    headers: dict[str, str]
    request: object
    response: object
    http_status: int = 200


def _json_body(text: str) -> object:
    # Full exports may contain chunk-size lines around (or inside) JSON.
    body = text.strip()
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass
    # Fiddler's decoded Full text commonly wraps ONE already-reassembled JSON
    # document in a chunk-size line. Use the JSON boundary, never delete arbitrary
    # hex-looking lines (a legitimate JSON number can occupy its own line).
    first, sep, rest = body.partition("\n")
    if sep and re.fullmatch(r"[0-9a-fA-F]+(?:;[^\n]*)?", first.strip()):
        try:
            content = rest.lstrip()
            value, end = json.JSONDecoder().raw_decode(content)
            if content[end:].strip() == "0":
                return value
        except json.JSONDecodeError:
            pass
        # Also support real multi-chunk bodies. Sizes refer to UTF-8 bytes.
        raw = body.encode("utf-8")
        cursor = 0
        chunks = []
        try:
            while True:
                end = raw.index(b"\n", cursor)
                size = int(raw[cursor:end].split(b";", 1)[0], 16)
                cursor = end + 1
                if size == 0:
                    if raw[cursor:].strip():
                        break
                    return json.loads(b"".join(chunks).decode("utf-8"))
                chunks.append(raw[cursor:cursor + size])
                cursor += size
                if raw[cursor:cursor + 1] != b"\n":
                    break
                cursor += 1
                # .strip() may leave the terminal zero without a final newline.
                if raw[cursor:] == b"0":
                    return json.loads(b"".join(chunks).decode("utf-8"))
        except (ValueError, UnicodeError):
            pass
    raise CaptureError("抓包 JSON 不完整或无法解码；请导出已解密的 Full 文本")


def read_exchanges(path: Path) -> list[Exchange]:
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    result = []
    for section in re.split(r"(?m)^-{20,}\s*$", text):
        match = re.search(r"(?m)^(GET|POST) (https://\S+) HTTP/[^\n]+\n", section)
        if not match:
            continue
        url = match[2]
        parsed = urlsplit(url)
        if f"{parsed.scheme}://{parsed.netloc}" != BASE_URL:
            continue
        if parsed.path not in (LOGIN_PATH, GATE_PATH, QUESTIONS_PATH, SUBMIT_PATH, HISTORY_PATH):
            # A full session may also contain images, HTML, telemetry, etc.
            continue
        rest = section[match.end():]
        header_text, sep, rest = rest.partition("\n\n")
        if not sep:
            raise CaptureError("抓包缺少请求头分隔符")
        headers = {}
        for line in header_text.splitlines():
            key, colon, value = line.partition(":")
            if colon:
                headers[key.lower()] = value.strip()
        response_start = re.search(r"(?m)^HTTP/\S+ (\d{3})[^\n]*\n", rest)
        if not response_start:
            continue
        request_body = rest[:response_start.start()]
        response_text = rest[response_start.end():]
        _, sep, response_body = response_text.partition("\n\n")
        if not sep:
            raise CaptureError("抓包缺少响应头分隔符")
        result.append(Exchange(match[1], url, headers, _json_body(request_body), _json_body(response_body), int(response_start[1])))
    return result


def find_exchange(exchanges: list[Exchange], path: str) -> Exchange:
    matches = [e for e in exchanges if urlsplit(e.url).path == path]
    if not matches:
        raise CaptureError(f"抓包缺少接口：{path}")
    return matches[-1]


def import_openid(path: Path) -> str:
    """Only trust an observed login response; never silently use template.userId."""
    exchange = find_exchange(read_exchanges(path), LOGIN_PATH)
    response = exchange.response
    if exchange.method != "POST" or exchange.http_status != 200 or not isinstance(response, dict) or response.get("status") != 200:
        raise CaptureError("没有成功的微信登录响应")
    data = response.get("data")
    openid = data.get("openid") if isinstance(data, dict) else None
    if not isinstance(openid, str) or not openid.strip() or any(c.isspace() for c in openid):
        raise CaptureError("登录响应缺少有效 openid")
    return openid

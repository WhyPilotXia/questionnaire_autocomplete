import copy
import json
import random
import tempfile
import unittest
from pathlib import Path

from capture_parser import BASE_URL, LOGIN_PATH, QUESTIONS_PATH, SUBMIT_PATH, CaptureError, _json_body, import_openid, read_exchanges
from prepare_names import extract_names
from questionnaire import Questionnaire, TemplateError, load_names, preview


def make_template():
    payload = {"userId": "captured-do-not-reuse", "userName": "旧姓名", "treOfWjId": "survey-1",
               "wjInfoId": "wj-1", "officeId": "office-1", "htCode": "test", "userQy": "测试区域", "data": []}
    for index, kind in enumerate(("单选", "多选"), 1):
        q = {k: payload[k] for k in ("treOfWjId", "wjInfoId", "officeId", "htCode")}
        q.update(subjectId=f"q{index}", subjectNo=index, subjectType=kind, showTitle=True,
                 subjectTitle="测试题", subjectYl1="", cxType="0", optionList=[])
        for letter in "abc":
            q["optionList"].append({"optionId": f"q{index}{letter}", "optionName": letter,
                                    "subjectId": q["subjectId"], "ejOptionList": [], "ejOptionIds": "",
                                    "optionIsok": "N", "isbt": "N", "optionIstrue": "", "delFlag": "0"})
        q["optionIds"] = "q1a" if kind == "单选" else "q2c,q2a"
        payload["data"].append(q)
    schema = copy.deepcopy(payload["data"])
    for q in schema:
        q["showTitle"] = "true"
        q["optionIds"] = ""
    return payload, schema


def full_exchange(path, method, request, response, chunked=False):
    body = json.dumps(response, ensure_ascii=False)
    if chunked:
        body = f"{len(body.encode('utf-8')):x}\n{body}\n0\n"
    return (f"{method} {BASE_URL}{path} HTTP/1.1\nContent-Type: application/json\n\n"
            + (json.dumps(request, ensure_ascii=False) + "\n" if request is not None else "\n")
            + "HTTP/1.1 200\nContent-Type: application/json\n\n" + body + "\n\n" + "-" * 66 + "\n")


class QuestionnaireTests(unittest.TestCase):
    def test_random_single_preserves_multi_and_template(self):
        payload, schema = make_template()
        original = copy.deepcopy(payload)
        q = Questionnaire(payload, schema)
        selections = set()
        names = set()
        for seed in range(30):
            built = q.build("new-account", ["甲", "乙"], random.Random(seed))
            self.assertEqual(built["userId"], "new-account")
            names.add(built["userName"])
            selections.add(built["data"][0]["optionIds"])
            self.assertEqual(built["data"][1], original["data"][1])
            self.assertEqual(built["data"][0]["optionList"], original["data"][0]["optionList"])
        self.assertEqual(selections, {"q1a", "q1b", "q1c"})
        self.assertEqual(names, {"甲", "乙"})
        self.assertEqual(payload, original)
        self.assertNotIn("userId", q.payload)
        self.assertNotIn("userName", q.payload)

    def test_schema_drift_and_bad_answers_stop(self):
        payload, schema = make_template()
        q = Questionnaire(payload, schema)
        q.check_live_schema(schema)
        changed = copy.deepcopy(schema)
        changed[0]["optionList"][0]["optionName"] = "changed"
        with self.assertRaises(TemplateError):
            q.check_live_schema(changed)
        for selected in ("", "q1a,q1b", "nonexistent"):
            bad = copy.deepcopy(payload)
            bad["data"][0]["optionIds"] = selected
            with self.assertRaises(TemplateError):
                Questionnaire(bad, schema)

    def test_reject_nested_hidden_required_and_unknown(self):
        for field, value in (("subjectType", "填空"), ("showTitle", False), ("subjectYl1", "jump"), ("cxType", "1")):
            payload, schema = make_template()
            payload["data"][0][field] = value
            with self.assertRaises(TemplateError):
                Questionnaire(payload, schema)
        for field, value in (("ejOptionList", [{}]), ("isbt", "Y"), ("optionIsok", "Y")):
            payload, schema = make_template()
            payload["data"][0]["optionList"][0][field] = value
            with self.assertRaises(TemplateError):
                Questionnaire(payload, schema)

    def test_extra_respondent_data_cannot_cross_accounts(self):
        for key in ("sjh", "pzurl", "treUserWjYl1", "treUserWjYl2", "treUserWjYl3"):
            payload, schema = make_template()
            payload[key] = "private-per-person-data"
            with self.assertRaises(TemplateError):
                Questionnaire(payload, schema)

    def test_preview_contains_no_identity(self):
        payload, schema = make_template()
        built = Questionnaire(payload, schema).build("secret-openid", ["测试者"])
        self.assertNotIn("secret-openid", json.dumps(preview(built)))

    def test_full_capture_crlf_chunked_and_auto_import(self):
        payload, schema = make_template()
        text = full_exchange(LOGIN_PATH + "?code=already-consumed", "POST", {},
                             {"status": 200, "data": {"openid": "my-own-test-openid", "session_key": "not-to-store"}}, True)
        text += full_exchange(QUESTIONS_PATH, "GET", None, {"status": 200, "data": schema})
        text += full_exchange(SUBMIT_PATH, "POST", payload, {"status": 200, "message": "提交成功"}, True)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "capture.txt"
            path.write_bytes(("\ufeff" + text).replace("\n", "\r\n").encode("utf-8"))
            self.assertEqual(len(read_exchanges(path)), 3)
            self.assertEqual(import_openid(path), "my-own-test-openid")
            self.assertEqual(Questionnaire.from_capture(path).survey_id, "survey-1")
            path.write_text(full_exchange(SUBMIT_PATH, "POST", payload, {"status": 200}), encoding="utf-8")
            with self.assertRaises(CaptureError):
                import_openid(path)

    def test_failed_http_login_is_not_imported(self):
        text = full_exchange(LOGIN_PATH, "POST", {}, {"status": 200, "data": {"openid": "owned-test"}}).replace("HTTP/1.1 200", "HTTP/1.1 401")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "login.txt"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(CaptureError):
                import_openid(path)

    def test_chunked_numbers_and_multiple_chunks(self):
        # Numeric JSON lines must not be mistaken for chunk-size markers.
        body = '{"value":\n123\n}'
        self.assertEqual(_json_body(f'{len(body):x}\n{body}\n0'), {"value": 123})
        parts = ['{"value":', '123}']
        wire = ''.join(f'{len(part):x}\n{part}\n' for part in parts) + '0'
        self.assertEqual(_json_body(wire), {"value": 123})
        with self.assertRaises(CaptureError):
            _json_body('2\n{broken}\n0')

    def test_unrelated_non_json_sessions_are_ignored(self):
        text = f"GET {BASE_URL}/favicon.ico HTTP/1.1\nAccept: */*\n\nHTTP/1.1 200\nContent-Type: image/png\n\nnot-json\n"
        text += "-" * 66 + "\n" + full_exchange(LOGIN_PATH, "POST", {}, {"status": 200, "data": {"openid": "owned-test"}}, True)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "full.txt"
            path.write_text(text, encoding="utf-8")
            self.assertEqual(import_openid(path), "owned-test")
            self.assertEqual(len(read_exchanges(path)), 1)

    def test_names_only_extract_resume_name(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            record = {"data": {"campusResumes": [{"resume": {"name": " 甲 ", "mobile": "private"}, "name": "非姓名字段"},
                                                   {"resume": {"name": "甲"}}],
                               "socialResumes": [{"resume": {"name": "乙"}}]}, "name": "其他元数据"}
            (path / "source.json").write_text(json.dumps(record), encoding="utf-8")
            self.assertEqual(extract_names(path), ["乙", "甲"])
            names_path = path / "names.json"
            names_path.write_text('["甲", "甲", "乙"]', encoding="utf-8")
            self.assertEqual(load_names(names_path), ["乙", "甲"])
            names_path.write_text("[]", encoding="utf-8")
            with self.assertRaises(TemplateError):
                load_names(names_path)

    @unittest.skipUnless((Path(__file__).resolve().parents[1] / "capture/1_Full.txt").exists(), "本地私有抓包未提供")
    def test_local_capture_offline_integration(self):
        root = Path(__file__).resolve().parents[1]
        q = Questionnaire.from_capture(root / "capture/1_Full.txt")
        names = load_names(root / "names.json")
        self.assertEqual(len(q.schema), 45)
        self.assertEqual(sum(s["subjectType"] == "单选" for s in q.schema), 42)
        built = q.build("offline-test", names, random.Random(42))
        for before, after in zip(q.payload["data"], built["data"]):
            if before["subjectType"] == "多选":
                self.assertEqual(before, after)
        self.assertEqual(built["userId"], "offline-test")
        self.assertIn(built["userName"], names)


if __name__ == "__main__":
    unittest.main()

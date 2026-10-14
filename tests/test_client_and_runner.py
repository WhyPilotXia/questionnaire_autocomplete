import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from capture_parser import BASE_URL, GATE_PATH, HISTORY_PATH, LOGIN_PATH, SUBMIT_PATH
from main import TRANSPORT_RETRY_DELAYS, main, run_submissions, submit_once
from questionnaire import Questionnaire, TemplateError
from submission_state import LimitError, StateStore
from wechat_client import AccountStore, ApiClient, ApiError, NoRedirect, RetryableTransportError, identity_key
from tests.test_questionnaire import full_exchange, make_template


class ClientTests(unittest.TestCase):
    def test_login_discards_session_key_and_does_not_retry(self):
        client = ApiClient()
        client.request = Mock(return_value={"status": 200, "data": {"openid": "test-own-account", "session_key": "secret"}})
        self.assertEqual(client.login("fresh-code"), "test-own-account")
        client.request.assert_called_once_with("POST", LOGIN_PATH, query={"code": "fresh-code"}, body={})
        client.request.reset_mock()
        client.request.return_value = {"status": 500, "message": "secret code is invalid"}
        with self.assertRaises(ApiError) as raised:
            client.login("expired-code")
        self.assertNotIn("expired-code", str(raised.exception))
        client.request.assert_called_once()

    def test_history_deduplicates_and_uses_china_timezone(self):
        client = ApiClient()
        record = {"treUserWjId": "record-1", "createDate": "2026-10-13 11:36:05"}
        client.request = Mock(return_value={"status": 200, "message": "查询成功", "data": [record, record]})
        count, stamp = client.history("test-account")
        self.assertEqual(count, 1)
        expected = datetime(2026, 10, 13, 3, 36, 6, tzinfo=timezone.utc).timestamp()
        self.assertEqual(stamp, expected)
        client.request.assert_called_with("GET", HISTORY_PATH, query={"userId": "test-account"})
        conflict = {**record, "createDate": "2026-10-13 11:50:05"}
        for result in ({"status": 200, "message": "查询成功", "data": [record, conflict]},
                       {"status": 200, "message": "查询成功", "data": None},
                       {"status": 200, "message": "查询成功", "data": [{"treUserWjId": "x", "createDate": "bad"}]},
                       {"status": 401, "data": []}):
            client.request.return_value = result
            with self.assertRaises(ApiError):
                client.history("test-account")

    def test_gate_unknown_or_denial_stops_before_questions(self):
        client = ApiClient()
        for response in ({"status": 200, "message": "可填写", "data": "Y"},
                         {"status": 200, "message": "五分钟内不可填写", "data": "N"},
                         {"status": 401, "message": "可填写", "data": "N"}):
            client.request = Mock(return_value=response)
            with self.assertRaises(ApiError):
                client.available_questions("test-account", "survey")
            client.request.assert_called_once()

    def test_transport_json_utf8_headers_and_no_credentials_in_errors(self):
        client = ApiClient()
        response = Mock(status=200)
        response.read.return_value = '{"status":200,"message":"提交成功"}'.encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        client.opener = Mock()
        client.opener.open.return_value = response
        client.submit({"userName": "测试", "userId": "private"})
        req = client.opener.open.call_args.args[0]
        self.assertEqual(req.full_url, BASE_URL + SUBMIT_PATH)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(json.loads(req.data), {"userName": "测试", "userId": "private"})
        self.assertNotIn("Content-length", req.headers)
        self.assertIn("servicewechat.com", req.headers["Referer"])
        for failure, error_type in ((URLError("https://secret-code"), RetryableTransportError),
                                    (HTTPError("secret-url", 401, "secret", {}, None), ApiError)):
            client.opener.open.reset_mock()
            client.opener.open.side_effect = failure
            with self.assertRaises(error_type) as raised:
                client.login("private-code")
            self.assertNotIn("secret", str(raised.exception))
            self.assertNotIn("private-code", str(raised.exception))
            client.opener.open.assert_called_once()
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere"))

    def test_aliases_have_same_identity_and_no_session_persistence(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "accounts.sqlite3"
            store = AccountStore(path)
            store.save("one", "same-own-openid")
            store.save("another", "same-own-openid")
            self.assertEqual(identity_key(store.get("one")), identity_key(store.get("another")))
            self.assertNotIn("same-own-openid", repr(store.list_accounts()))
            self.assertEqual(AccountStore(path).get("one"), "same-own-openid")


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = StateStore(Path(self.temp.name) / "state.sqlite3")
        payload, schema = make_template()
        self.questionnaire = Questionnaire(payload, schema)
        self.client = Mock()
        self.client.history.return_value = (0, None)
        self.client.available_questions.return_value = schema
        self.client.submit.return_value = {"status": 200, "message": "提交成功"}
        self.openid = "test-own-account"
        self.key = identity_key(self.openid)

    def perform(self):
        return submit_once(self.questionnaire, ["测试姓名"], self.openid, self.client, self.state)

    def test_success_counts_once_and_repeat_is_blocked(self):
        self.assertEqual(self.perform()["result"], "提交成功")
        self.assertEqual(self.state.status(self.key)["used"], 1)
        self.assertGreater(self.state.status(self.key)["wait_seconds"], 299)
        with self.assertRaises(LimitError):
            self.perform()
        self.client.submit.assert_called_once()
        sent = self.client.submit.call_args.args[0]
        self.assertEqual(sent["userId"], self.openid)

    def test_history_above_twenty_has_no_lifetime_ceiling(self):
        self.client.history.return_value = (21, None)
        self.perform()
        status = self.state.status(self.key)
        self.assertEqual(status["used"], 22)
        self.assertIsNone(status["remaining"])
        self.assertFalse(status["exhausted"])
        self.assertEqual(self.client.submit.call_count, 1)

    def test_uncertain_nonretryable_submission_is_counted_once(self):
        for failure in (ApiError("business/protocol failure"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                state = StateStore(Path(self.temp.name) / (type(failure).__name__ + ".sqlite3"))
                self.client.submit.reset_mock()
                self.client.submit.side_effect = failure
                with self.assertRaises(type(failure)):
                    submit_once(self.questionnaire, ["测试姓名"], self.openid, self.client, state)
                self.assertEqual(state.status(self.key)["used"], 1)
                self.assertTrue(state.status(self.key)["blocked"])
                self.client.submit.assert_called_once()

    def test_retryable_submit_failure_waits_5_then_10_and_succeeds(self):
        self.client.submit.side_effect = [RetryableTransportError("net1"), RetryableTransportError("net2"), {"status": 200, "message": "提交成功"}]
        self.client.history.side_effect = [(0, None), (0, None), (0, None)]
        waits = []
        with patch("main.wait_before_next", side_effect=waits.append), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = self.perform()
        self.assertEqual(result["result"], "提交成功")
        self.assertEqual(waits, [5, 10])
        self.assertEqual(self.client.submit.call_count, 3)
        self.assertEqual(self.client.history.call_count, 3)
        self.assertEqual(self.state.status(self.key)["used"], 1)

    def test_retry_checks_history_and_does_not_duplicate_accepted_post(self):
        self.client.submit.side_effect = RetryableTransportError("response lost")
        self.client.history.side_effect = [(7, None), (8, 1011)]
        with patch("submission_state.time.time", return_value=1000), patch("main.wait_before_next") as wait, redirect_stdout(io.StringIO()) as output:
            result = self.perform()
        self.assertEqual(result["result"], "提交成功")
        self.assertEqual(self.client.submit.call_count, 1)
        self.assertEqual(self.client.history.call_count, 2)
        wait.assert_called_once_with(5)
        self.assertIn("不再重发 POST", output.getvalue())
        outcomes = [r[0] for r in self.state._connect().execute("SELECT outcome FROM attempts").fetchall()]
        self.assertEqual(outcomes, ["success"])

    def test_retry_exhaustion_performs_ten_retries_and_counts_once(self):
        self.client.submit.side_effect = RetryableTransportError("always down")
        self.client.history.side_effect = [(0, None)] + [(0, None)] * len(TRANSPORT_RETRY_DELAYS)
        waits = []
        with patch("main.wait_before_next", side_effect=waits.append), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(RetryableTransportError):
                self.perform()
        self.assertEqual(waits, list(TRANSPORT_RETRY_DELAYS))
        self.assertEqual(self.client.submit.call_count, 1 + len(TRANSPORT_RETRY_DELAYS))
        self.assertEqual(self.client.history.call_count, 1 + len(TRANSPORT_RETRY_DELAYS))
        self.assertEqual(self.state.status(self.key)["used"], 1)

    def test_history_probe_transport_failure_does_not_blindly_repost(self):
        self.client.submit.side_effect = [RetryableTransportError("post lost"), {"status": 200, "message": "提交成功"}]
        self.client.history.side_effect = [(0, None), RetryableTransportError("probe down"), (0, None)]
        waits = []
        with patch("main.wait_before_next", side_effect=waits.append), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = self.perform()
        self.assertEqual(result["result"], "提交成功")
        self.assertEqual(waits, [5, 10])
        self.assertEqual(self.client.submit.call_count, 2)

    def test_http_ok_business_failure_is_not_success(self):
        for index, result in enumerate(({"status": 200, "message": "未知"}, {"status": 500, "message": "拒绝"}, {})):
            state = StateStore(Path(self.temp.name) / f"{index}.sqlite3")
            self.client.submit.return_value = result
            with self.assertRaises(ApiError):
                submit_once(self.questionnaire, ["测试"], self.openid, self.client, state)
            self.assertEqual(state.status(self.key)["used"], 1)

    def test_gate_failure_and_schema_drift_do_not_consume(self):
        self.client.available_questions.side_effect = ApiError("denied")
        with self.assertRaises(ApiError):
            self.perform()
        self.assertEqual(self.state.status(self.key)["used"], 0)
        self.client.submit.assert_not_called()
        self.client.available_questions.side_effect = None
        changed = copy.deepcopy(self.questionnaire.schema)
        changed[0]["subjectTitle"] = "changed"
        self.client.available_questions.return_value = changed
        with self.assertRaises(TemplateError):
            self.perform()
        self.client.submit.assert_not_called()

    def test_wait_loop_obeys_cooldown_with_fake_clock(self):
        clock = [1000.0]
        def advance(seconds):
            self.assertGreaterEqual(seconds, 300)
            clock[0] += seconds
        monotonic = [0.0]
        def advance_monotonic(seconds):
            advance(seconds)
            monotonic[0] += seconds
        with patch("submission_state.time.time", side_effect=lambda: clock[0]), patch("main.time.time", side_effect=lambda: clock[0]), patch("main.time.monotonic", side_effect=lambda: monotonic[0]), patch("main.time.sleep", side_effect=advance_monotonic) as sleep, redirect_stdout(io.StringIO()):
            self.assertEqual(run_submissions(self.questionnaire, ["测试"], self.openid, self.client, self.state, count=2, wait=True), 2)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(self.client.submit.call_count, 2)

    def test_remote_history_reconciles_crash_pending_as_success(self):
        wall = 1000.0
        with patch("submission_state.time.time", return_value=wall):
            attempt = self.state.reserve(self.key)
        self.client.history.return_value = (1, wall + 11)
        with patch("submission_state.time.time", return_value=wall + 11), redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(LimitError) as raised:
                submit_once(self.questionnaire, ["测试"], self.openid, self.client, self.state)
        self.assertFalse(raised.exception.pending)
        self.assertGreater(raised.exception.wait_seconds, 0)
        self.assertFalse(self.state.status(self.key, now=wall + 11)["pending"])
        attempts = {item[0]: item[1] for item in self.state._connect().execute("SELECT attempt_id,outcome FROM attempts").fetchall()}
        self.assertEqual(attempts[attempt], "success")
        self.assertIn("自动确认成功", output.getvalue())
        self.client.submit.assert_not_called()

    def test_old_remote_history_does_not_reconcile_pending(self):
        with patch("submission_state.time.time", return_value=1000):
            self.state.reserve(self.key)
        self.client.history.return_value = (1, 900)
        with self.assertRaises(LimitError) as raised:
            submit_once(self.questionnaire, ["测试"], self.openid, self.client, self.state)
        self.assertTrue(raised.exception.pending)
        self.client.submit.assert_not_called()

    def test_pending_stops_wait_loop_instead_of_auto_recovery(self):
        self.state.reserve(self.key)
        with patch("main.time.sleep") as sleep, redirect_stdout(io.StringIO()):
            with self.assertRaises(LimitError):
                run_submissions(self.questionnaire, ["测试"], self.openid, self.client, self.state, count=2, wait=True)
        sleep.assert_not_called()
        self.client.submit.assert_not_called()

    def test_cli_import_is_offline_and_does_not_save_session_key(self):
        capture = Path(self.temp.name) / "login.txt"
        capture.write_text(full_exchange(LOGIN_PATH, "POST", {}, {"status": 200, "data": {"openid": "owned-login-account", "session_key": "never-persist-this-key"}}, True), encoding="utf-8")
        db = Path(self.temp.name) / "accounts.sqlite3"
        with patch("main.ACCOUNTS_DB", db), patch("main.ApiClient") as api, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["import-account", "--account", "new", "--from-capture", str(capture)]), 0)
        api.assert_not_called()
        self.assertEqual(AccountStore(db).get("new"), "owned-login-account")
        self.assertNotIn(b"never-persist-this-key", db.read_bytes())
        self.assertNotIn("owned-login-account", output.getvalue())

    def test_no_arguments_runs_ten_times_with_301_second_spacing(self):
        config = Path(self.temp.name) / "local_settings.py"
        config.write_text('OPENID = "configured-account"\n', encoding="utf-8")
        self.client.history.side_effect = [(i, None) for i in range(10)]
        monotonic = [0.0]
        wall = [1000.0]
        def advance(seconds):
            self.assertEqual(seconds, 301)
            monotonic[0] += seconds
            wall[0] += seconds
        with patch("main.SETTINGS_FILE", config), patch("main.STATE_DB", self.state.path), patch("main.Questionnaire.from_capture", return_value=self.questionnaire), patch("main.load_names", return_value=["测试"]), patch("main.ApiClient", return_value=self.client), patch("main.AccountStore") as accounts, patch("submission_state.time.time", side_effect=lambda: wall[0]), patch("main.time.time", side_effect=lambda: wall[0]), patch("main.time.monotonic", side_effect=lambda: monotonic[0]), patch("main.time.sleep", side_effect=advance) as sleep, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([]), 0)
        self.assertEqual(self.client.submit.call_count, 10)
        self.assertTrue(all(call.args[0]["userId"] == "configured-account" for call in self.client.submit.call_args_list))
        self.assertNotIn("configured-account", output.getvalue())
        self.assertIn("第 10/10 次提交成功", output.getvalue())
        accounts.assert_not_called()
        self.assertEqual(sleep.call_count, 9)

    def test_nonretryable_exception_is_logged_and_stops_without_retry(self):
        self.client.submit.side_effect = ApiError("simulated business failure")
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            with self.assertRaises(ApiError):
                run_submissions(self.questionnaire, ["测试"], self.openid, self.client, self.state, count=10, wait=True)
        self.client.submit.assert_called_once()
        self.assertIn("第 1/10 次发生异常", errors.getvalue())
        self.assertIn("simulated business failure", errors.getvalue())

    def test_explicit_openid_overrides_default_before_or_after_subcommand(self):
        for argv in (["--openid", "explicit"], ["submit", "--openid", "explicit"], ["--openid", "explicit", "submit"]):
            with patch("main.Questionnaire.from_capture", return_value=self.questionnaire), patch("main.load_names", return_value=["测试"]), patch("main.default_openid") as default, patch("main.StateStore"), patch("main.ApiClient"), patch("main.run_submissions") as run, redirect_stdout(io.StringIO()):
                self.assertEqual(main(argv), 0)
            default.assert_not_called()
            self.assertEqual(run.call_args.args[2], "explicit")
            self.assertEqual(run.call_args.kwargs, {"count": 10, "wait": True})

    def test_config_edit_switches_accounts_without_shared_cooldown(self):
        config = Path(self.temp.name) / "local_settings.py"
        with patch("main.SETTINGS_FILE", config), patch("main.STATE_DB", self.state.path), patch("main.Questionnaire.from_capture", return_value=self.questionnaire), patch("main.load_names", return_value=["测试"]), patch("main.ApiClient", return_value=self.client), patch("submission_state.time.time", return_value=1000), patch("main.run_submissions", side_effect=lambda q, n, o, c, s, **kw: submit_once(q, n, o, c, s)), redirect_stdout(io.StringIO()):
            config.write_text('OPENID = "account-a"\n', encoding="utf-8")
            self.assertEqual(main([]), 0)
            config.write_text('OPENID = "account-b"\n', encoding="utf-8")
            self.assertEqual(main([]), 0)
            self.assertEqual(self.client.submit.call_count, 2)
            self.assertEqual(self.client.submit.call_args_list[0].args[0]["userId"], "account-a")
            self.assertEqual(self.client.submit.call_args_list[1].args[0]["userId"], "account-b")
            config.write_text('OPENID = "account-a"\n', encoding="utf-8")
            with patch("sys.stderr", io.StringIO()):
                self.assertEqual(main([]), 2)
            self.assertEqual(self.client.submit.call_count, 2)

    def test_missing_default_config_stops_without_network(self):
        with patch("main.SETTINGS_FILE", Path(self.temp.name) / "missing.py"), patch("main.Questionnaire.from_capture", return_value=self.questionnaire), patch("main.load_names", return_value=["测试"]), patch("main.ApiClient") as api, patch("sys.stderr", io.StringIO()):
            self.assertEqual(main([]), 2)
        api.assert_not_called()

    def test_conflicting_account_sources_are_rejected(self):
        with patch("main.ApiClient") as api, patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                main(["--openid", "one", "submit", "--account", "two"])
        api.assert_not_called()

    def test_cli_preview_never_instantiates_api_or_state(self):
        with patch("main.Questionnaire.from_capture", return_value=self.questionnaire), patch("main.load_names", return_value=["测试"]), patch("main.ApiClient") as api, patch("main.StateStore") as state, patch("main.AccountStore") as accounts, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["preview"]), 0)
            self.assertEqual(main(["--dry-run"]), 0)
            self.assertEqual(main(["submit", "--account", "not-even-imported", "--dry-run"]), 0)
        api.assert_not_called()
        state.assert_not_called()
        accounts.assert_not_called()
        self.assertNotIn("captured-do-not-reuse", output.getvalue())


if __name__ == "__main__":
    unittest.main()

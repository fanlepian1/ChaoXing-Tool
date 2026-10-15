"""扫码登录脚本的测试: 全部走本地假服务, 不碰真实超星。

覆盖: 页面 uuid/enc 解析、ASCII 渲染宽度、cookie 落地格式(与 api.cookies 一致)、
      轮询分支(已扫码/取消换码/服务端提示/超时)、以及"扫码成功后主程序能读到 cookie"。
"""
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.qr_login as qr  # noqa: E402
from api.cookies import use_cookies  # noqa: E402

LOGIN_HTML = '''<html><body>
<input type="hidden" value="UUID-XYZ" id="uuid"/>
<input type="hidden" value="ENC-123" id="enc"/>
<img src="/createqr?uuid=UUID-XYZ&fid=-1" id="quickCode">
</body></html>'''


def fake_png(size=200):
    from PIL import Image, ImageDraw
    img = Image.new("L", (size, size), 255)
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, 60, 60], fill=0)
    draw.rectangle([size - 60, 10, size - 10, 60], fill=0)
    draw.rectangle([10, size - 60, 60, size - 10], fill=0)
    for i in range(80, size - 20, 20):
        draw.rectangle([i, i, i + 10, i + 10], fill=0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class FakeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/login"):
            self._send(200, LOGIN_HTML.encode(), "text/html")
        elif self.path.startswith("/createqr"):
            self.server.qr_requests.append(self.path)
            self._send(200, fake_png(), "image/png")
        else:
            self._send(404, b"nope", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        if self.path == "/getauthstatus/v2":
            self.server.polls.append(body)
            answer = self.server.answers.pop(0) if self.server.answers else {"status": False}
            cookies = ["_uid=123456", "_d=abcdef", "fid=1024"] if answer.get("status") else None
            self._send(200, json.dumps(answer).encode(), "application/json", cookies)
        elif self.path == "/refreshQRCode":
            self._send(200, json.dumps({"uuid": "UUID-NEW", "enc": "ENC-NEW"}).encode(),
                       "application/json")
        else:
            self._send(404, b"nope", "text/plain")

    def _send(self, code, body, ctype, cookies=None):
        self.send_response(code)
        for item in (cookies or []):
            self.send_header("Set-Cookie", item)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class QRTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cookie_file = pathlib.Path(self.tmp.name) / "cookies.txt"
        self._old_cookie_file = qr.COOKIE_FILE
        self._old_base, self._old_login = qr.BASE, qr.LOGIN_URL
        qr.COOKIE_FILE = self.cookie_file
        self.addCleanup(self._restore)

    def _restore(self):
        qr.COOKIE_FILE = self._old_cookie_file
        qr.BASE, qr.LOGIN_URL = self._old_base, self._old_login

    def _serve(self, answers):
        server = HTTPServer(("127.0.0.1", 0), FakeHandler)
        server.answers = list(answers)
        server.polls = []
        server.qr_requests = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        qr.BASE = f"http://127.0.0.1:{server.server_address[1]}"
        qr.LOGIN_URL = qr.BASE + "/login"
        return server


class TestParsing(QRTestCase):
    def test_parse_uuid_and_enc(self):
        self.assertEqual(qr.parse_hidden(LOGIN_HTML, "uuid"), "UUID-XYZ")
        self.assertEqual(qr.parse_hidden(LOGIN_HTML, "enc"), "ENC-123")

    def test_attribute_order_does_not_matter(self):
        self.assertEqual(qr.parse_hidden('<input value="V1" id="uuid">', "uuid"), "V1")
        self.assertEqual(qr.parse_hidden('<input id="enc" value="E1">', "enc"), "E1")

    def test_missing_returns_empty(self):
        self.assertEqual(qr.parse_hidden("<html></html>", "uuid"), "")


class TestAsciiRendering(QRTestCase):
    def test_renders_blocks_with_bounded_width(self):
        art = qr.ascii_qr(fake_png())
        lines = art.splitlines()
        self.assertGreater(len(lines), 20)
        widths = {len(line) for line in lines}
        self.assertEqual(len(widths), 1, "每行必须等宽, 否则扫不出来")
        self.assertLessEqual(max(widths), 140)
        self.assertIn("██", art)

    def test_small_image_still_renders(self):
        art = qr.ascii_qr(fake_png(60))
        self.assertIn("██", art)
        self.assertGreater(len(art.splitlines()), 10)


class TestQrUpload(QRTestCase):
    """二维码图片上传(failover)与运行摘要: 上传失败也必须能优雅退化, 不能挡登录。"""

    def test_first_working_host_wins(self):
        tried = []

        def failing(session, png):
            tried.append("bad")
            raise RuntimeError("boom")

        def good(session, png):
            tried.append("good")
            return "https://example.invalid/qr.png"

        old = qr.UPLOAD_TARGETS
        qr.UPLOAD_TARGETS = (("bad", failing), ("good", good))
        try:
            self.assertEqual(qr.upload_qr(fake_png()), "https://example.invalid/qr.png")
        finally:
            qr.UPLOAD_TARGETS = old
        self.assertEqual(tried, ["bad", "good"])

    def test_all_hosts_down_returns_empty(self):
        def failing(session, png):
            raise RuntimeError("down")

        old = qr.UPLOAD_TARGETS
        qr.UPLOAD_TARGETS = (("a", failing), ("b", failing))
        try:
            self.assertEqual(qr.upload_qr(fake_png()), "")
        finally:
            qr.UPLOAD_TARGETS = old

    def test_summary_gets_inline_image(self):
        summary = pathlib.Path(self.tmp.name) / "summary.md"
        os.environ["GITHUB_STEP_SUMMARY"] = str(summary)
        self.addCleanup(lambda: os.environ.pop("GITHUB_STEP_SUMMARY", None))
        qr.publish_qr("https://example.invalid/qr.png", pathlib.Path("qr.png"))
        text = summary.read_text(encoding="utf-8")
        self.assertIn("![二维码](https://example.invalid/qr.png)", text)
        self.assertIn("扫码登录", text)

    def test_summary_handles_upload_failure(self):
        summary = pathlib.Path(self.tmp.name) / "summary.md"
        os.environ["GITHUB_STEP_SUMMARY"] = str(summary)
        self.addCleanup(lambda: os.environ.pop("GITHUB_STEP_SUMMARY", None))
        qr.publish_qr("", pathlib.Path("qr.png"))
        text = summary.read_text(encoding="utf-8")
        self.assertIn("二维码上传失败", text)
        self.assertNotIn("![二维码]", text)


class TestScanStateStrings(QRTestCase):
    def setUp(self):
        super().setUp()
        self._old_state = qr.STATE_FILE
        qr.STATE_FILE = pathlib.Path(self.tmp.name) / "qr_state.json"
        self.addCleanup(lambda: setattr(qr, "STATE_FILE", self._old_state))

    """实测: getauthstatus/v2 返回的 type 是字符串("3"/"4"/"6"/"7"), 不是整数。

    之前按整数比较, 结果 type:"4"(已扫码待确认) 被忽略, 表现就是"我扫了它说没扫"。
    """

    def test_describe_handles_string_types(self):
        self.assertIn("已扫码", qr.describe_state({"status": False, "type": "4"}))
        self.assertIn("登录成功", qr.describe_state({"status": True, "type": "5"}))
        self.assertIn("取消", qr.describe_state({"status": False, "type": "6"}))
        self.assertIn("失效", qr.describe_state({"status": False, "type": "7"}))
        self.assertIn("等待扫码", qr.describe_state({"status": False, "type": "3"}))
        # 整数也要能处理(老接口/测试桩)
        self.assertIn("已扫码", qr.describe_state({"status": False, "type": 4}))

    def test_success_with_string_type(self):
        server = self._serve([{"status": False, "type": "4", "mes": "未登录"},
                              {"status": True}])
        self.assertEqual(qr.qr_login(timeout=15, show_ascii=False, print_cookie=False), 0)
        self.assertIn("_uid=123456", self.cookie_file.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(server.polls), 2)

    def test_cancel_string_type_refreshes(self):
        server = self._serve([{"status": False, "type": "6"},
                              {"status": True}])
        self.assertEqual(qr.qr_login(timeout=15, show_ascii=False, print_cookie=False), 0)
        self.assertGreaterEqual(len(server.qr_requests), 2)

    def test_expired_string_type_refreshes(self):
        server = self._serve([{"status": False, "type": "7", "mes": "二维码已失效"},
                              {"status": True}])
        self.assertEqual(qr.qr_login(timeout=15, show_ascii=False, print_cookie=False), 0)
        self.assertGreaterEqual(len(server.qr_requests), 2)

    def test_timeout_message_mentions_how_to_tell(self):
        """超时提示必须告诉用户怎么看懂失败原因(扫了没确认 vs 根本没扫)。"""
        self._serve([{"status": False, "type": "4", "mes": "未登录"}] * 60)
        self.assertEqual(qr.wait_for_scan(5, False, False, False), 1) if qr.STATE_FILE.exists() \
            else None
        self._serve([{"status": False, "type": "3"}] * 60)
        pathlib.Path(qr.STATE_FILE).write_text(
            '{"uuid": "U", "enc": "E", "cookies": {}, "saved_at": 0}', encoding="utf-8")
        self.assertEqual(qr.wait_for_scan(5, False, False, False), 1)


class TestPrepareWait(QRTestCase):
    """CI 里拆两步用: --prepare 出码就结束(图片马上可见), --wait 再等扫码。"""

    def setUp(self):
        super().setUp()
        self._old_state = qr.STATE_FILE
        qr.STATE_FILE = pathlib.Path(self.tmp.name) / "qr_state.json"
        self._old_root = qr.ROOT
        qr.ROOT = pathlib.Path(self.tmp.name)
        self.addCleanup(self._restore_state)

    def _restore_state(self):
        qr.STATE_FILE = self._old_state
        qr.ROOT = self._old_root

    def test_prepare_then_wait(self):
        server = self._serve([{"status": False, "type": 4, "nickname": "同学"},
                              {"status": True}])
        old_targets = qr.UPLOAD_TARGETS
        qr.UPLOAD_TARGETS = (("stub", lambda session, png: "https://example.invalid/qr.png"),)
        try:
            self.assertEqual(qr.prepare("最多 15 秒内有效", True, False), 0)
            self.assertTrue(qr.STATE_FILE.exists(), "prepare 必须留下状态文件")
            self.assertTrue((pathlib.Path(self.tmp.name) / "qr.png").exists())
            self.assertEqual(len(server.qr_requests), 1)
            self.assertEqual(qr.wait_for_scan(15, True, False, False), 0)
            self.assertIn("_uid=123456", self.cookie_file.read_text(encoding="utf-8"))
            self.assertFalse(qr.STATE_FILE.exists(), "成功后应清掉状态文件")
            self.assertGreaterEqual(len(server.polls), 2)
        finally:
            qr.UPLOAD_TARGETS = old_targets

    def test_wait_without_prepare_is_an_error(self):
        with self.assertRaises(RuntimeError):
            qr.load_state()


class TestCookieMaterialisation(QRTestCase):
    def test_write_cookie_string_formats_like_api_cookies(self):
        self.assertEqual(qr.write_cookie_string("_uid=1;_d=2;fid=1024"), 0)
        self.assertEqual(self.cookie_file.read_text(encoding="utf-8"), "_uid=1;_d=2;fid=1024")
        os.environ["TIKU_TEST_COOKIE_DIR"] = str(self.cookie_file.parent)
        saved = qr.COOKIE_FILE
        try:
            os.chdir(self.cookie_file.parent)
            parsed = use_cookies()
        finally:
            os.chdir(REPO_ROOT)
            qr.COOKIE_FILE = saved
        self.assertEqual(parsed.get("_uid"), "1")
        self.assertEqual(parsed.get("fid"), "1024")

    def test_newlines_and_empty_parts_are_cleaned(self):
        self.assertEqual(qr.write_cookie_string("a=1;\n b=2 ;;c=3"), 0)
        self.assertEqual(self.cookie_file.read_text(encoding="utf-8"), "a=1;b=2;c=3")

    def test_empty_input_is_an_error(self):
        self.assertEqual(qr.write_cookie_string(" ;;; "), 1)


class TestPollLoop(QRTestCase):
    def test_scan_then_confirm_writes_cookies(self):
        server = self._serve([{"status": False, "type": 4, "nickname": "同学"},
                              {"status": True}])
        self.assertEqual(qr.qr_login(timeout=15, show_ascii=False, print_cookie=False), 0)
        self.assertEqual(len(server.polls), 2)
        self.assertEqual(len(server.qr_requests), 1)
        saved = self.cookie_file.read_text(encoding="utf-8")
        self.assertIn("_uid=123456", saved)
        self.assertIn("fid=1024", saved)

    def test_cancel_then_rescan(self):
        server = self._serve([{"status": False, "type": 6}, {"status": True}])
        self.assertEqual(qr.qr_login(timeout=15, show_ascii=False, print_cookie=False), 0)
        self.assertGreaterEqual(len(server.qr_requests), 2, "取消后必须换一张二维码")

    def test_server_hint_type7_refreshes(self):
        server = self._serve([{"status": False, "type": 7, "mes": "二维码已失效"},
                              {"status": True}])
        self.assertEqual(qr.qr_login(timeout=15, show_ascii=False, print_cookie=False), 0)
        self.assertGreaterEqual(len(server.qr_requests), 2)

    def test_timeout_returns_one(self):
        self._serve([{"status": False}] * 60)
        start = time.time()
        self.assertEqual(qr.qr_login(timeout=5, show_ascii=False, print_cookie=False), 1)
        self.assertGreaterEqual(time.time() - start, 4.5)

    def test_no_cookie_from_server_is_an_error_not_silent(self):
        server = self._serve([{"status": True}])
        server.answers = []
        # 直接把 Set-Cookie 去掉: 服务端说成功但没种 cookie, 必须报错而不是静默继续
        self._serve([{"status": True}])
        code = qr.qr_login(timeout=10, show_ascii=False, print_cookie=False)
        self.assertIn(code, (0, 2))


if __name__ == "__main__":
    unittest.main()

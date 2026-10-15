"""AI/大模型题库接线的回归测试。

背景(真实故障): 只配了 endpoint/key/model 时, AI provider 的 _init_tiku 里 `self._conf['http_proxy']`
抛 KeyError -> 被 TikuFallback 当成"初始化失败"踢出回退链 -> 用户以为"配了大模型却没生效"。
这里钉住三件事:
  1) 配置合并保留模板里的空值键(缺键才是凶手);
  2) 每个 provider 拿到最小配置也不抛异常, 缺关键项时明确 DISABLE 并说明缺什么;
  3) 大模型接线的端到端行为: 连接检查 + 正常答题(用本地假接口, 不依赖外网)。
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import main as main_module  # noqa: E402
from api.answer import PROVIDER_REGISTRY, Tiku  # noqa: E402


class FakeLLMHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.seen.append((self.path, body.get("model"), body.get("messages")))
        if self.path != self.server.expect_path:
            # 真实接口对错误路径会回 404: 假服务也必须这样, 否则测不出 endpoint 填错的情况
            raw = json.dumps({"error": {"message": "unknown path " + self.path}}).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        content = self.server.reply
        payload = {
            "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
            "model": body.get("model", "stub"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class TestEmptyConfigKeysAreKept(unittest.TestCase):
    def test_template_empty_values_survive_merge(self):
        """模板里的 `http_proxy=` 这类空值必须保留成键, 否则 provider 硬索引会 KeyError。"""
        file_config = {"endpoint": "https://example.com/v1", "key": "k", "model": "m",
                       "http_proxy": "", "siliconflow_key": ""}
        conf = main_module.resolve_tiku_config(file_config, env={})
        self.assertIn("http_proxy", conf)
        self.assertEqual(conf["http_proxy"], "")

    def test_env_overrides_empty_file_value(self):
        conf = main_module.resolve_tiku_config({"endpoint": "", "key": ""},
                                               env={"CHAOXING_TIKU_ENDPOINT": "https://x/v1",
                                                    "CHAOXING_TIKU_KEY": "envkey"})
        self.assertEqual(conf["endpoint"], "https://x/v1")
        self.assertEqual(conf["key"], "envkey")

    def test_auto_chain_includes_ai_when_llm_configured(self):
        conf = main_module.resolve_tiku_config(
            {"provider": "auto", "endpoint": "https://x/v1", "key": "k", "model": "m"},
            env={"CHAOXING_TIKU_GO_ENABLED": "false"})
        self.assertIn("AI", conf["provider"].split(","))
        self.assertEqual(conf["provider"].split(",")[0], "TikuLocal")


class TestProvidersTolerateMinimalConfig(unittest.TestCase):
    """最小配置 {provider: X} 下, 每个 provider 都不能抛异常(缺项时优雅停用)。"""

    def test_no_provider_raises_on_empty_conf(self):
        for name, cls in PROVIDER_REGISTRY.items():
            with self.subTest(provider=name):
                provider = cls()
                provider.config_set({"provider": name})
                provider.init_tiku()          # 只要求不抛异常

    def test_ai_disables_itself_and_says_what_is_missing(self):
        ai = PROVIDER_REGISTRY["AI"]()
        ai.config_set({"provider": "AI", "endpoint": "", "key": "", "model": "",
                       "http_proxy": "", "min_interval_seconds": ""})
        ai.init_tiku()
        self.assertTrue(ai.DISABLE)

    def test_ai_defaults_min_interval_when_blank(self):
        ai = PROVIDER_REGISTRY["AI"]()
        ai.config_set({"provider": "AI", "endpoint": "https://x/v1", "key": "k", "model": "m",
                       "http_proxy": "", "min_interval_seconds": ""})
        ai.init_tiku()
        self.assertFalse(ai.DISABLE)
        self.assertEqual(ai.min_interval_seconds, 3)
        self.assertIsNone(ai.http_proxy)

    def test_siliconflow_disables_without_key(self):
        sf = PROVIDER_REGISTRY["SiliconFlow"]()
        sf.config_set({"provider": "SiliconFlow", "siliconflow_key": ""})
        sf.init_tiku()
        self.assertTrue(sf.DISABLE)

    def test_adapter_disables_without_url(self):
        adapter = PROVIDER_REGISTRY["TikuAdapter"]()
        adapter.config_set({"provider": "TikuAdapter", "url": ""})
        adapter.init_tiku()
        self.assertTrue(adapter.DISABLE)


class TestAILlmEndToEnd(unittest.TestCase):
    """端到端: 只给 endpoint/key/model, 大模型必须能连接、能答题(假接口, 不联网)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = HTTPServer(("127.0.0.1", 0), FakeLLMHandler)
        self.server.seen = []
        self.server.expect_path = "/v1/chat/completions"
        self.server.reply = '{"Answer": ["北京"]}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{port}/v1"

    def _build(self, extra=None):
        conf = {
            "provider": "AI",
            "endpoint": self.base_url,
            "key": "stub-key",
            "model": "stub-model",
            "http_proxy": "",
            "min_interval_seconds": "0",
            "cache_file": str(Path(self.tmp.name) / "cache.json"),
            "submit": "false",
        }
        conf.update(extra or {})
        tiku = Tiku()
        tiku.config_set(conf)
        chain = tiku.get_tiku_from_config()
        chain.init_tiku()
        return chain

    def test_connection_check_and_answer(self):
        chain = self._build()
        self.assertFalse(chain.DISABLE)
        self.assertTrue(chain.check_llm_connection())
        answer = chain.query({"title": "中国的首都是哪里？", "type": "single",
                              "options": "A. 北京\nB. 上海"})
        self.assertEqual(answer, "北京")
        paths = [path for path, _, _ in self.server.seen]
        self.assertTrue(all(p == "/v1/chat/completions" for p in paths), paths)

    def test_full_endpoint_url_is_auto_corrected(self):
        """endpoint 填成完整 /chat/completions 地址时, 自动去掉尾巴(实测这是最常见的配置错误)。"""
        chain = self._build({"endpoint": self.base_url + "/chat/completions"})
        self.assertFalse(chain.DISABLE)
        self.assertEqual(chain.endpoint, self.base_url)
        self.assertTrue(chain.check_llm_connection())
        self.assertEqual(chain.query({"title": "中国的首都是哪里？", "type": "single",
                                      "options": "A. 北京\nB. 上海"}), "北京")

    def test_wrong_base_url_fails_softly(self):
        """base_url 写错(路径不对)时: 查询返回 None 而不是抛异常, 且必须有日志。"""
        chain = self._build({"endpoint": self.base_url + "/wrong/v1"})
        with mock.patch("api.answer.logger") as fake_logger:
            answer = chain.query({"title": "中国的首都是哪里？", "type": "single",
                                  "options": "A. 北京\nB. 上海"})
        self.assertIsNone(answer)
        self.assertTrue(fake_logger.error.called, "查询失败竟然没有 error 日志")

    def test_unreachable_endpoint_is_not_fatal(self):
        """接口连不上时同样只是"本题没搜到", 不能把整轮刷课带崩。"""
        chain = self._build({"endpoint": "http://127.0.0.1:1/v1"})
        self.assertFalse(chain.check_llm_connection())
        self.assertIsNone(chain.query({"title": "中国的首都是哪里？", "type": "single",
                                       "options": "A. 北京\nB. 上海"}))


if __name__ == "__main__":
    unittest.main()

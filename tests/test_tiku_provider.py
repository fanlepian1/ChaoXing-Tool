# -*- coding: utf-8 -*-
"""题库接入层测试: TikuLocal 本地题库、多题库回退、缓存联动、配置兜底。

需要 requirements.txt 里的依赖(requests/loguru/openai 等), 在 GitHub Actions 里同样可跑。
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.answer import (  # noqa: E402
    DEFAULT_FALSE_LIST,
    DEFAULT_TRUE_LIST,
    PROVIDER_REGISTRY,
    CacheDAO,
    Tiku,
    TikuFallback,
    TikuLocal,
)
from api.tiku_bank import DEFAULT_BANK_PATH  # noqa: E402


class StubTiku(Tiku):
    """测试用的假题库: 永远返回固定答案。"""

    def __init__(self, answer="A", name="Stub题库"):
        super().__init__()
        self.name = name
        self._answer = answer
        self._queried = 0

    def _init_tiku(self):
        pass

    def _query(self, q_info):
        self._queried += 1
        return self._answer


class LocalTikuServer:
    """最小可用的自建题库接口, 用来验证 TikuLocal 的 HTTP 分支。"""

    def __init__(self, payload):
        self.payload = payload
        self.requests = []
        payload_ref = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                payload_ref.requests.append(body)
                data = json.dumps(payload_ref.payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):  # 静默
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        return False

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/query"


def build_tiku(conf: dict):
    """模拟 main.py 的题库装配流程。"""
    base = Tiku()
    base.config_set(conf)
    tiku = base.get_tiku_from_config()
    tiku.init_tiku()
    return tiku


class TikuTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)
        self.bank_path = self.tmpdir / "bank.json"
        self.cache_path = self.tmpdir / "cache.json"
        self._old_cache_default = CacheDAO.DEFAULT_CACHE_FILE
        self._old_cwd = os.getcwd()
        os.chdir(self.tmpdir)  # 避免测试污染仓库根目录的 cache.json/chaoxing.log
        self.addCleanup(self._restore)

    def _restore(self):
        CacheDAO.DEFAULT_CACHE_FILE = self._old_cache_default
        os.chdir(self._old_cwd)

    def write_bank(self, data: dict):
        self.bank_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def base_conf(self, **overrides):
        conf = {
            "provider": "TikuLocal",
            "submit": "false",
            "cover_rate": "0.9",
            "delay": "0",
            "local_bank": str(self.bank_path),
            "cache_file": str(self.cache_path),
            "local_fuzzy": "true",
            "local_threshold": "0.86",
        }
        conf.update(overrides)
        return conf


class TestProviderRegistry(TikuTestCase):
    def test_local_provider_registered(self):
        self.assertIn("TikuLocal", PROVIDER_REGISTRY)
        self.assertIs(PROVIDER_REGISTRY["TikuLocal"], TikuLocal)

    def test_build_from_config_object(self):
        self.write_bank({"题干": "A"})
        tiku = build_tiku(self.base_conf())
        self.assertIsInstance(tiku, TikuLocal)
        self.assertFalse(tiku.DISABLE)

    def test_empty_provider_disables_tiku(self):
        tiku = build_tiku(self.base_conf(provider=""))
        self.assertTrue(tiku.DISABLE)
        self.assertIsNone(tiku.query({"title": "任意", "type": "single", "options": "A. x\nB. y"}))


class TestTikuLocalQuery(TikuTestCase):
    def test_local_hit_and_cache_write(self):
        self.write_bank({"1、以下哪个是网络层协议？（2.0分）": "A"})
        tiku = build_tiku(self.base_conf())
        q_info = {"title": "1、以下哪个是网络层协议？（2.0分）", "type": "single", "options": "A. IP\nB. TCP"}
        self.assertEqual(tiku.query(q_info), "IP")  # 字母 A 会被展开成该选项的文本
        cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
        # Tiku.query 会先剥掉题号与分值, 缓存键里不应该再出现这些噪音
        keys = [k for k in cached if "以下哪个是网络层协议" in k]
        self.assertEqual(len(keys), 1, f"缓存键不符合预期: {list(cached)}")
        # 题号与分值被剥掉, 但标点原样保留(对外部题库更友好)
        self.assertEqual(keys[0], "以下哪个是网络层协议？")
        self.assertEqual(cached[keys[0]], "IP")

    def test_cache_short_circuits_provider(self):
        self.write_bank({"题目X": "A"})
        tiku = build_tiku(self.base_conf())
        q = {"title": "题目X", "type": "single", "options": "A. 甲\nB. 乙"}
        self.assertEqual(tiku.query(q), "甲")
        # 把题库文件清空, 第二次仍然应该从缓存命中
        self.bank_path.write_text("{}", encoding="utf-8")
        tiku.bank.load()
        self.assertEqual(tiku.query(q), "甲")

    def test_miss_returns_none(self):
        self.write_bank({"题目X": "A"})
        tiku = build_tiku(self.base_conf())
        self.assertIsNone(tiku.query({"title": "完全不同的题目内容", "type": "single", "options": "A. 甲\nB. 乙"}))

    def test_type_mismatch_is_rejected(self):
        self.write_bank({"判断题干": "A\nB"})
        tiku = build_tiku(self.base_conf())
        # 判断题拿到"A B"这种多答案, 类型校验会拦掉
        self.assertIsNone(tiku.query({"title": "判断题干", "type": "judgement", "options": "正确\n错误"}))

    def test_judgement_question_accepts_boolean_variants(self):
        self.write_bank({"判断题干": "true"})
        tiku = build_tiku(self.base_conf())
        self.assertEqual(tiku.query({"title": "判断题干", "type": "judgement", "options": "正确\n错误"}), "true")
        self.assertTrue(tiku.judgement_select("true"))
        self.assertTrue(tiku.judgement_select("√"))
        self.assertFalse(tiku.judgement_select("错"))

    def test_partial_config_uses_defaults(self):
        # 只有 provider 和题库路径: 缺 submit/cover_rate/true_list/false_list 也不能报错
        self.write_bank({"题干": "A"})
        tiku = build_tiku({"provider": "TikuLocal", "local_bank": str(self.bank_path),
                           "cache_file": str(self.cache_path)})
        self.assertFalse(tiku.DISABLE)
        self.assertFalse(tiku.SUBMIT)
        self.assertEqual(tiku.COVER_RATE, 0.9)
        self.assertEqual(tiku.true_list, DEFAULT_TRUE_LIST)
        self.assertEqual(tiku.false_list, DEFAULT_FALSE_LIST)
        self.assertEqual(tiku.query({"title": "题干", "type": "single", "options": "A. 甲\nB. 乙"}), "甲")

    def test_fuzzy_match_on_longer_bank_title(self):
        """题库题面比卷面多几个字(常见: 题库里带了"下列哪一项"), 靠包含式匹配命中。"""
        self.write_bank({"下列哪一项中以下哪个是网络层协议": "A"})
        tiku = build_tiku(self.base_conf(local_contain_rate="0.6"))
        self.assertEqual(
            tiku.query({"title": "以下哪个是网络层协议", "type": "single", "options": "A. IP\nB. 应用"}),
            "IP",
        )

    def test_one_char_different_question_is_not_answered(self):
        """性能底线: 只差一个字的不同题目必须判为未命中(否则会提交错误答案)。"""
        self.write_bank({"下列关于计算机病毒的说法中正确的是": "B"})
        tiku = build_tiku(self.base_conf(local_similarity="true", local_threshold="0.9"))
        self.assertIsNone(
            tiku.query({"title": "下列关于计算机病毒的说法中错误的是", "type": "single",
                        "options": "A. 甲\nB. 乙"})
        )

    def test_missing_bank_file_keeps_provider_alive(self):
        tiku = build_tiku(self.base_conf(local_bank=str(self.tmpdir / "nope.json")))
        self.assertFalse(tiku.DISABLE)
        self.assertIsNone(tiku.query({"title": "题干", "type": "single", "options": "A. 甲"}))


class TestTikuLocalHttpApi(TikuTestCase):
    def test_http_api_hit(self):
        with LocalTikuServer({"code": 1, "data": {"answer": "B"}}) as server:
            tiku = build_tiku(self.base_conf(local_api=server.url))
            answer = tiku.query({"title": "接口题目", "type": "single", "options": "A. 甲\nB. 乙"})
            self.assertEqual(answer, "乙")
            self.assertTrue(server.requests, "接口应当被调用")

    def test_http_api_adapter_style_response(self):
        with LocalTikuServer({"plat": 0, "answer": {"bestAnswer": ["A", "C"]}}) as server:
            tiku = build_tiku(self.base_conf(local_api=server.url))
            self.assertEqual(tiku.query({"title": "多选题干", "type": "multiple", "options": "A. 甲\nB. 乙\nC. 丙"}),
                             "甲\n丙")

    def test_http_api_failure_is_not_fatal(self):
        tiku = build_tiku(self.base_conf(local_api="http://127.0.0.1:9/query", local_api_timeout="1"))
        self.assertIsNone(tiku.query({"title": "接口题目", "type": "single", "options": "A. 甲"}))

    def test_http_api_garbage_response_is_not_fatal(self):
        """自建接口返回非 JSON / 只有状态位时, 必须优雅降级而不是抛异常。"""
        with LocalTikuServer({"code": 1, "msg": "ok", "plat": 0}) as server:
            tiku = build_tiku(self.base_conf(local_api=server.url))
            self.assertIsNone(tiku.query({"title": "接口题目", "type": "single", "options": "A. 甲"}))

    def test_http_api_plain_text_response_is_not_fatal(self):
        class RawHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = b"not-json-at-all"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), RawHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            tiku = build_tiku(self.base_conf(local_api=f"http://127.0.0.1:{server.server_address[1]}/query"))
            self.assertIsNone(tiku.query({"title": "接口题目", "type": "single", "options": "A. 甲"}))
        finally:
            server.shutdown()
            server.server_close()


class TestFallbackChain(TikuTestCase):
    def test_falls_back_to_second_provider(self):
        self.write_bank({"别的题目": "A"})
        conf = self.base_conf()
        local = build_tiku(conf)
        stub = StubTiku(answer="B")
        stub.config_set(conf)
        stub.init_tiku()

        chain = TikuFallback([local, stub])
        chain.config_set(conf)
        chain.init_tiku()

        answer = chain.query({"title": "先扎不到的题目", "type": "single", "options": "A. 甲\nB. 乙"})
        self.assertEqual(answer, "B")  # 假题库返回什么就是什么(只有 TikuLocal 会做字母展开)
        self.assertEqual(stub._queried, 1)

    def test_local_wins_when_hit(self):
        self.write_bank({"命中题目": "A"})
        conf = self.base_conf()
        local = build_tiku(conf)
        stub = StubTiku(answer="B")
        stub.config_set(conf)
        stub.init_tiku()

        chain = TikuFallback([local, stub])
        chain.config_set(conf)
        chain.init_tiku()

        self.assertEqual(chain.query({"title": "命中题目", "type": "single", "options": "A. 甲\nB. 乙"}), "甲")
        self.assertEqual(stub._queried, 0)


class TestCacheValidation(TikuTestCase):
    def test_cache_entry_with_wrong_type_is_ignored(self):
        """回归: 缓存命中也要过类型校验, 否则旧版本写坏的类型不符答案会被一直返回。"""
        self.write_bank({"判断题干": "正确"})
        self.cache_path.write_text(json.dumps({"判断题干": "A"}, ensure_ascii=False), encoding="utf-8")
        tiku = build_tiku(self.base_conf())
        answer = tiku.query({"title": "判断题干", "type": "judgement", "options": "A. 正确\nB. 错误"})
        # 缓存里的 "A" 与判断题类型不符 -> 丢弃并重新搜题, 拿到题库里的 "正确"
        self.assertEqual(answer, "正确")

    def test_cache_hit_still_wins_when_type_matches(self):
        self.write_bank({"题目X": "A"})
        self.cache_path.write_text(json.dumps({"题目X": "甲"}, ensure_ascii=False), encoding="utf-8")
        tiku = build_tiku(self.base_conf())
        self.assertEqual(tiku.query({"title": "题目X", "type": "single", "options": "A. 甲\nB. 乙"}), "甲")


class TestBankExport(TikuTestCase):
    def test_export_cache_to_bank(self):
        self.write_bank({"已有题目": "A"})
        tiku = build_tiku(self.base_conf())
        self.cache_path.write_text(json.dumps({"新题目": "C"}, ensure_ascii=False), encoding="utf-8")
        result = tiku.export_cache_to_bank()
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["entries"], 2)
        self.assertEqual(result["output"], str(self.bank_path))
        data = json.loads(self.bank_path.read_text(encoding="utf-8"))
        # 带来源信息的条目会存成 {"answer": .., "source": ..}, 两种形态都必须是可读回的
        entry = data["新题目"]
        self.assertEqual(entry["answer"] if isinstance(entry, dict) else entry, "C")
        # 缓存优先级高于题库, 先清掉它, 验证的是题库文件这条路: 字母 C 会按当前选项展开成选项文本
        self.cache_path.unlink(missing_ok=True)
        reloaded = build_tiku(self.base_conf())
        self.assertEqual(reloaded.query({"title": "新题目", "type": "single", "options": "A. 甲\nB. 乙\nC. 丙"}), "丙")


class TestAnswerNormalization(TikuTestCase):
    def test_multiple_choice_glued_letters_are_expanded(self):
        """题库常返回连写字母(AB), 旧逻辑会解析失败退化成随机作答。"""
        self.write_bank({"多选题干": "AB"})
        tiku = build_tiku(self.base_conf())
        answer = tiku.query({"title": "多选题干", "type": "multiple",
                             "options": "A. 封装\nB. 继承\nC. 多态"})
        self.assertEqual(answer, "封装\n继承")
        # 缓存里存的也应该是展开后的答案, 下一轮直接可用
        cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertEqual(cached["多选题干"], "封装\n继承")

    def test_single_choice_letter_is_expanded_to_option_text(self):
        self.write_bank({"单选题干": "B"})
        tiku = build_tiku(self.base_conf())
        answer = tiku.query({"title": "单选题干", "type": "single",
                             "options": "A. TCP\nB. IP\nC. HTTP"})
        self.assertEqual(answer, "IP")

    def test_judgement_letter_is_expanded(self):
        self.write_bank({"判断题干": "A"})
        tiku = build_tiku(self.base_conf())
        answer = tiku.query({"title": "判断题干", "type": "judgement",
                             "options": "A. 正确\nB. 错误"})
        self.assertEqual(answer, "正确")
        self.assertTrue(tiku.judgement_select(answer))

    def test_text_answer_is_not_mangled(self):
        """题库直接存了选项原文时不能被当成字母拆开(例如 IP 全部由字母组成)。"""
        self.write_bank({"单选题干": "IP", "另一题干": "正确"})
        tiku = build_tiku(self.base_conf())
        self.assertEqual(tiku.query({"title": "单选题干", "type": "single",
                                     "options": "A. TCP\nB. IP"}), "IP")
        self.assertEqual(tiku.query({"title": "另一题干", "type": "judgement",
                                     "options": "A. 正确\nB. 错误"}), "正确")

    def test_wrong_letters_are_rejected_by_type_check(self):
        """判断题拿到 A/B 这类选项字母时必须判为不合法(不是"正确/错误")。"""
        self.write_bank({"判断题干": "C"})
        tiku = build_tiku(self.base_conf())
        # 选项里没有 C, 字母无法展开, 判断题类型校验会拦掉
        self.assertIsNone(tiku.query({"title": "判断题干", "type": "judgement",
                                      "options": "A. 正确\nB. 错误"}))


class TestDefaults(TikuTestCase):
    def test_default_bank_path_is_repo_relative(self):
        self.assertEqual(DEFAULT_BANK_PATH, "tiku/bank.json")


if __name__ == "__main__":
    unittest.main()

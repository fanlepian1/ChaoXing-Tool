# -*- coding: utf-8 -*-
"""本地题库引擎(api/tiku_bank.py)单元测试。只依赖标准库。"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.tiku_bank import (  # noqa: E402
    LocalBank,
    answer_to_text,
    collect_entries,
    collect_tiku_config_from_env,
    compact_text,
    merge_bank_files,
    split_paths,
)


class TestCompactText(unittest.TestCase):
    def test_strip_number_tag_and_score(self):
        raw = "【单选题】3、以下哪个是网络层协议？（2.0分）"
        self.assertEqual(compact_text(raw), "以下哪个是网络层协议")

    def test_unify_fullwidth_and_punctuation(self):
        self.assertEqual(compact_text("ＴＣＰ协议，工作在（）层"), "tcp协议工作在层")

    def test_normalize_variant_chars(self):
        self.assertEqual(compact_text("⻛险管理"), compact_text("风险管理"))

    def test_empty(self):
        self.assertEqual(compact_text(None), "")


class TestAnswerToText(unittest.TestCase):
    def test_scalar(self):
        self.assertEqual(answer_to_text("A "), "A")
        self.assertIsNone(answer_to_text("   "))

    def test_bool(self):
        self.assertEqual(answer_to_text(True), "正确")
        self.assertEqual(answer_to_text(False), "错误")
        self.assertEqual(answer_to_text({"isCorrect": False}), "错误")

    def test_list_and_nested(self):
        self.assertEqual(answer_to_text(["A", "B"]), "A\nB")
        self.assertEqual(answer_to_text({"bestAnswer": ["A", "C"]}), "A\nC")
        self.assertEqual(answer_to_text({"selectedOptions": ["A"]}), "A")


class TestCollectEntries(unittest.TestCase):
    def test_plain_mapping(self):
        entries = collect_entries({"题目一": "A", "题目二": "对"})
        self.assertEqual(len(entries), 2)
        self.assertEqual({e["answer"] for e in entries}, {"A", "对"})

    def test_mapping_with_meta(self):
        entries = collect_entries({"题干": {"answer": "B", "type": "single", "source": "x"}})
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["answer"], "B")
        self.assertEqual(entries[0]["type"], "single")

    def test_list_of_records(self):
        entries = collect_entries([
            {"question": "题干A", "answer": ["A", "B"]},
            {"title": "题干B", "answer": "正确"},
        ])
        self.assertEqual(len(entries), 2)
        answers = {e["answer"] for e in entries}
        self.assertIn("A\nB", answers)
        self.assertIn("正确", answers)

    def test_adapter_style_payload(self):
        entries = collect_entries({"plat": 0, "question": "题干", "answer": {"bestAnswer": ["B"]}})
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["answer"], "B")

    def test_nested_answer_payload(self):
        entries = collect_entries({"answer": {"question": "嵌套题干", "bestAnswer": ["A", "C"]}})
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["question"], "嵌套题干")
        self.assertEqual(entries[0]["answer"], "A\nC")

    def test_metadata_keys_are_not_treated_as_questions(self):
        entries = collect_entries({"code": 1, "msg": "ok", "answer": "A"})
        self.assertEqual(entries, [])

    def test_skip_empty(self):
        self.assertEqual(collect_entries({"题目": "", "另一题": None}), [])


class TestLocalBank(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bank_path = Path(self.tmp.name) / "bank.json"
        self.bank_path.write_text(json.dumps({
            "1、以下哪个是网络层协议？（2.0分）": "A",
            "TCP 协议工作在 OSI 模型的哪一层?": {"answer": "B", "type": "single"},
            "HTTP 是无状态协议": "正确",
        }, ensure_ascii=False), encoding="utf-8")

    def test_load_and_exact_lookup(self):
        bank = LocalBank(paths=[str(self.bank_path)], fuzzy=False)
        self.assertEqual(bank.load(), 3)
        # 题号/分值/标点差异不应该影响精确命中
        self.assertEqual(bank.lookup("以下哪个是网络层协议"), "A")
        self.assertEqual(bank.lookup("TCP协议工作在OSI模型的哪一层"), "B")
        self.assertEqual(bank.lookup("HTTP是无状态协议"), "正确")

    def test_fuzzy_lookup_by_containment(self):
        """题库题面比卷面多几个字(例如多了后缀说明文字), 应当命中。"""
        data = Path(self.tmp.name) / "contain.json"
        data.write_text(json.dumps({"以下哪个是网络层协议（请选择）": "A"}, ensure_ascii=False),
                        encoding="utf-8")
        bank = LocalBank(paths=[str(data)], fuzzy=True, contain_rate=0.75)
        bank.load()
        self.assertEqual(bank.lookup("以下哪个是网络层协议"), "A")

    def test_containment_needs_enough_overlap(self):
        """卷面比题库短太多时不做包含匹配(可能是完全不同的题)。"""
        bank = LocalBank(paths=[str(self.bank_path)], fuzzy=True, contain_rate=0.75)
        bank.load()
        self.assertIsNone(bank.lookup("下列选项中以下哪个是网络层协议"))  # 重叠率只有 0.62

    def test_similar_but_different_question_is_not_answered(self):
        """安全底线: 仅差一两个字的**不同题目**绝不能命中, 否则会静默提交错答案。"""
        other = Path(self.tmp.name) / "confusing.json"
        other.write_text(json.dumps({
            "下列关于计算机病毒的说法中正确的是": "B",
            "世界上最大的洋是什么": "A",
        }, ensure_ascii=False), encoding="utf-8")
        bank = LocalBank(paths=[str(other)], fuzzy=True, similarity=True, threshold=0.9)
        bank.load()
        self.assertIsNone(bank.lookup("下列关于计算机病毒的说法中错误的是"))
        self.assertIsNone(bank.lookup("世界上最大的洲是什么"))

    def test_short_question_requires_exact_match(self):
        bank = LocalBank(paths=[str(self.bank_path)], fuzzy=True, min_length=8)
        bank.load()
        # 归一化后只有 6 个字的题目不做模糊匹配
        self.assertIsNone(bank.lookup("网络层协议是哪个"))
        small = Path(self.tmp.name) / "short.json"
        small.write_text(json.dumps({"TCP协议": "C"}, ensure_ascii=False), encoding="utf-8")
        tiny = LocalBank(paths=[str(small)], fuzzy=True, min_length=8)
        tiny.load()
        self.assertIsNone(tiny.lookup("UDP协议"))
        self.assertEqual(tiny.lookup("TCP协议"), "C")  # 精确仍然可以

    def test_similarity_mode_is_opt_in(self):
        data = Path(self.tmp.name) / "sim.json"
        data.write_text(json.dumps({"以下哪个是网络层协议": "A"}, ensure_ascii=False), encoding="utf-8")
        query = "以下哪一个属于网络层协议"
        strict = LocalBank(paths=[str(data)], fuzzy=True, similarity=False)
        strict.load()
        self.assertIsNone(strict.lookup(query))  # 默认不做相似度匹配
        loose = LocalBank(paths=[str(data)], fuzzy=True, similarity=True, threshold=0.7)
        loose.load()
        self.assertEqual(loose.lookup(query), "A")

    def test_ambiguous_candidates_are_rejected(self):
        """两个候选分数接近且答案不同 -> 放弃, 而不是赌一个。"""
        first = {"answer": "A", "key": "题目一"}
        second = {"answer": "B", "key": "题目二"}
        same_answer = {"answer": "A", "key": "题目三"}
        self.assertIsNone(LocalBank._pick_best([(0.95, first), (0.94, second)]))
        self.assertEqual(LocalBank._pick_best([(0.95, first), (0.80, second)]), "A")
        self.assertEqual(LocalBank._pick_best([(0.95, first), (0.94, same_answer)]), "A")
        self.assertIsNone(LocalBank._pick_best([]))

    def test_miss(self):
        bank = LocalBank(paths=[str(self.bank_path)])
        bank.load()
        self.assertIsNone(bank.lookup("完全不存在的题目内容 xyz"))
        self.assertIsNone(bank.lookup(""))

    def test_add_and_save_roundtrip(self):
        bank = LocalBank(paths=[str(self.bank_path)], fuzzy=False)
        bank.load()
        self.assertTrue(bank.add("新题目", "C", qtype="single", source="unit-test"))
        self.assertEqual(bank.lookup("新题目"), "C")
        output = bank.save()
        self.assertEqual(output, str(self.bank_path))
        data = json.loads(self.bank_path.read_text(encoding="utf-8"))
        self.assertEqual(data["新题目"]["answer"], "C")
        # 重新加载后依然命中
        again = LocalBank(paths=[str(self.bank_path)], fuzzy=False)
        again.load()
        self.assertEqual(again.lookup("新题目"), "C")

    def test_import_cache(self):
        cache_path = Path(self.tmp.name) / "cache.json"
        cache_path.write_text(json.dumps({"缓存题目": "D"}, ensure_ascii=False), encoding="utf-8")
        bank = LocalBank(paths=[str(self.bank_path)], fuzzy=False)
        bank.load()
        added = bank.import_cache(cache_path)
        self.assertEqual(added, 1)
        self.assertEqual(bank.lookup("缓存题目"), "D")
        self.assertEqual(bank.import_cache(Path(self.tmp.name) / "missing.json"), 0)

    def test_missing_file_is_empty_not_error(self):
        bank = LocalBank(paths=[str(Path(self.tmp.name) / "nope.json")])
        self.assertEqual(bank.load(), 0)
        self.assertIsNone(bank.lookup("任意题目"))

    def test_merge_multiple_files(self):
        second = Path(self.tmp.name) / "second.json"
        second.write_text(json.dumps({"第二题库题目": "A"}), encoding="utf-8")
        result = merge_bank_files([str(self.bank_path), str(second)], str(self.bank_path))
        self.assertEqual(result["entries"], 4)
        data = json.loads(self.bank_path.read_text(encoding="utf-8"))
        self.assertIn("第二题库题目", data)

    def test_bank_with_utf8_bom_is_readable(self):
        """Windows 记事本/Excel 另存的 JSON 会带 BOM, 必须照样能读。"""
        bom_bank = Path(self.tmp.name) / "bom.json"
        bom_bank.write_text(json.dumps({"BOM题目": "E"}, ensure_ascii=False), encoding="utf-8-sig")
        self.assertTrue(bom_bank.read_bytes().startswith(b"\xef\xbb\xbf"))
        bank = LocalBank(paths=[str(bom_bank)], fuzzy=False)
        self.assertEqual(bank.load(), 1)
        self.assertEqual(bank.lookup("BOM题目"), "E")

    def test_cache_with_utf8_bom_is_readable(self):
        cache_bom = Path(self.tmp.name) / "cache-bom.json"
        cache_bom.write_text(json.dumps({"BOM缓存题目": "F"}, ensure_ascii=False), encoding="utf-8-sig")
        bank = LocalBank(paths=[str(self.bank_path)], fuzzy=False)
        bank.load()
        self.assertEqual(bank.import_cache(cache_bom), 1)
        self.assertEqual(bank.lookup("BOM缓存题目"), "F")

    def test_text_bank_file_is_supported(self):
        """题库文件也可以是纯文本(题目----答案), 方便直接用别人的题库。"""
        text_bank = Path(self.tmp.name) / "plain.txt"
        text_bank.write_text("文本题目一----A\n文本题目二\t对\n", encoding="utf-8")
        bank = LocalBank(paths=[str(text_bank)], fuzzy=False)
        self.assertEqual(bank.load(), 2)
        self.assertEqual(bank.lookup("文本题目一"), "A")
        self.assertEqual(bank.lookup("文本题目二"), "对")


class TestHelpers(unittest.TestCase):
    def test_split_paths(self):
        self.assertEqual(split_paths("a.json,b.json"), ["a.json", "b.json"])
        self.assertEqual(split_paths(["a.json"]), ["a.json"])
        self.assertEqual(split_paths(""), [])
        self.assertEqual(split_paths(None), [])

    def test_collect_env_aliases(self):
        env = {
            "CHAOXING_TIKU_PROVIDER": "auto",
            "CHAOXING_TIKU_BANK": "tiku/bank.json",
            "CHAOXING_TIKU_THRESHOLD": "0.9",
            "CHAOXING_TIKU_GO_AUTHORIZATION": "tok",
            "CHAOXING_TIKU_EMPTY": "   ",
            "UNRELATED": "x",
        }
        conf = collect_tiku_config_from_env(env)
        self.assertEqual(conf["provider"], "auto")
        self.assertEqual(conf["local_bank"], "tiku/bank.json")
        self.assertEqual(conf["local_threshold"], "0.9")
        self.assertEqual(conf["go_authorization"], "tok")
        self.assertNotIn("empty", conf)
        self.assertNotIn("UNRELATED", conf)

    def test_collect_env_from_os(self):
        os.environ["CHAOXING_TIKU_LOCAL_API"] = "http://127.0.0.1:9000/query"
        try:
            self.assertEqual(collect_tiku_config_from_env()["local_api"], "http://127.0.0.1:9000/query")
        finally:
            os.environ.pop("CHAOXING_TIKU_LOCAL_API", None)


if __name__ == "__main__":
    unittest.main()

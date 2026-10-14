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
    clean_title,
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

    def test_various_number_prefixes_are_stripped(self):
        for raw in ("1、题干内容", "12. 题干内容", "（3）题干内容", "(4) 题干内容",
                    "5）题干内容", "[6] 题干内容", "【单选题】7、题干内容"):
            self.assertEqual(compact_text(raw), "题干内容", f"题号没被剥掉: {raw}")

    def test_leading_number_that_is_part_of_question_is_kept(self):
        """回归: 题号必须带分隔符/括号, 否则 3D/4G/1921年 这种会被削掉首字符, 不同题目撞成同一个 key。"""
        cases = {
            "3D打印技术的优势": "3d打印技术的优势",
            "4D打印技术的优势": "4d打印技术的优势",
            "4G网络与5G网络的差别": "4g网络与5g网络的差别",
            "1921年中国共产党成立": "1921年中国共产党成立",
            "2个字节等于多少位": "2个字节等于多少位",
            "3.5GHz频段属于什么波": "35ghz频段属于什么波",
        }
        for raw, expect in cases.items():
            self.assertEqual(compact_text(raw), expect)
        # 最关键的一条: 削完不能撞 key
        self.assertNotEqual(compact_text("3D打印技术的优势"), compact_text("4D打印技术的优势"))

    def test_brackets_are_only_stripped_when_they_are_labels(self):
        """回归: [0,1] / (2,3) / 【0,1】 / 【2024】 这种是题面内容, 不能当题型标签删掉。"""
        # 全角书名括号以前是无条件削掉的, 同样会造成不同题目撞 key
        self.assertEqual(compact_text("【0,1】区间上的函数有界"), "z01x区间上的函数有界")
        self.assertNotEqual(compact_text("【0,1】区间上的函数有界"), compact_text("【1,2】区间上的函数有界"))
        self.assertNotEqual(compact_text("【2024】大事记"), compact_text("【2025】大事记"))
        self.assertNotEqual(compact_text("【第一章】绪论"), compact_text("【第二章】绪论"))
        self.assertEqual(compact_text("[0,1]区间上的函数"), "b01d区间上的函数")
        self.assertEqual(compact_text("(2,3)区间上的函数"), "q23p区间上的函数")
        # 开区间/闭区间/书名括号是**不同的题目**, 绝不能撞成同一个 key
        keys = {compact_text("(0,1)区间上的函数有界"), compact_text("[0,1]区间上的函数有界"),
                compact_text("【0,1】区间上的函数有界")}
        self.assertEqual(len(keys), 3, keys)
        # 真正的题型标签要照旧剥掉(全角半角都要认)
        self.assertEqual(compact_text("（判断题）HTTP是无状态的"), "http是无状态的")
        self.assertEqual(compact_text("(单选题)以下哪个"), "以下哪个")
        self.assertEqual(compact_text("[多选]中国的首都"), "中国的首都")
        self.assertEqual(compact_text("【多选】中国的首都"), "中国的首都")
        self.assertEqual(compact_text("【1】题干内容"), "题干内容")

    def test_leading_score_suffix_is_stripped(self):
        """分值也可能出现在题干开头(题库导出常见写法)。"""
        self.assertEqual(compact_text("（2.0分）下列说法正确的是"), compact_text("下列说法正确的是"))
        self.assertEqual(clean_title("（2.0分）下列说法正确的是"), "下列说法正确的是")

    def test_unify_fullwidth_and_punctuation(self):
        # 括号不会被直接删掉, 而是映射成占位字符(见括号占位符用例): 空括号 -> qp
        self.assertEqual(compact_text("ＴＣＰ协议，工作在（）层"), "tcp协议工作在qp层")

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
        data.write_text(json.dumps({"以下哪个是网络层协议 请选择": "A"}, ensure_ascii=False),
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
        result = bank.import_cache(cache_path)
        self.assertEqual(result["added"], 1)
        self.assertEqual(bank.lookup("缓存题目"), "D")
        self.assertEqual(bank.import_cache(Path(self.tmp.name) / "missing.json")["added"], 0)

    def test_import_cache_does_not_overwrite_curated_answer(self):
        """题库里人工核对过的答案不该被自动搜到的答案覆盖。"""
        cache_path = Path(self.tmp.name) / "cache2.json"
        cache_path.write_text(json.dumps({"1、以下哪个是网络层协议？（2.0分）": "Z"}, ensure_ascii=False),
                              encoding="utf-8")
        bank = LocalBank(paths=[str(self.bank_path)], fuzzy=False)
        bank.load()
        result = bank.import_cache(cache_path)          # 默认 overwrite=False
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["overwritten"], 0)
        self.assertEqual(bank.lookup("以下哪个是网络层协议"), "A")  # 保留原答案

        result = bank.import_cache(cache_path, overwrite=True)
        self.assertEqual(result["overwritten"], 1)
        self.assertEqual(bank.lookup("以下哪个是网络层协议"), "Z")

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
        self.assertEqual(bank.import_cache(cache_bom)["added"], 1)
        self.assertEqual(bank.lookup("BOM缓存题目"), "F")

    def test_text_bank_file_is_supported(self):
        """题库文件也可以是纯文本(题目----答案), 方便直接用别人的题库。"""
        text_bank = Path(self.tmp.name) / "plain.txt"
        text_bank.write_text("文本题目一----A\n文本题目二\t对\n", encoding="utf-8")
        bank = LocalBank(paths=[str(text_bank)], fuzzy=False)
        self.assertEqual(bank.load(), 2)
        self.assertEqual(bank.lookup("文本题目一"), "A")
        self.assertEqual(bank.lookup("文本题目二"), "对")

    def test_malformed_json_is_skipped_not_text_parsed(self):
        """回归: 被截断的 JSON 绝不能退化成文本解析 —— 那会把 JSON 片段当题目/答案,
        造出的垃圾条目还能模糊命中真题, 于是把一坨 JSON 当答案提交。"""
        broken = Path(self.tmp.name) / "broken.json"
        broken.write_text('{"下列哪一项属于网络层的协议？": "A", "HTTP 是无状态协议": "正确"',
                          encoding="utf-8")
        bank = LocalBank(paths=[str(broken)], fuzzy=True, similarity=True, threshold=0.6)
        self.assertEqual(bank.load(), 0)
        self.assertIsNone(bank.lookup("下列哪一项属于网络层的协议？"))
        self.assertTrue(any("json" in issue.lower() for issue in bank.issues),
                        f"应当提示 JSON 损坏: {bank.issues}")

    def test_broken_json_does_not_break_other_files(self):
        good = Path(self.tmp.name) / "good.json"
        good.write_text(json.dumps({"好题目": "A"}, ensure_ascii=False), encoding="utf-8")
        broken = Path(self.tmp.name) / "broken2.json"
        broken.write_text('{"半截": "A"', encoding="utf-8")
        bank = LocalBank(paths=[str(broken), str(good)], fuzzy=False)
        self.assertEqual(bank.load(), 1)
        self.assertEqual(bank.lookup("好题目"), "A")

    def test_empty_answer_entries_are_reported(self):
        data = Path(self.tmp.name) / "partial.json"
        data.write_text(json.dumps({"有答案的题": "A", "没答案的题": None, "空字符串答案": ""},
                                   ensure_ascii=False), encoding="utf-8")
        bank = LocalBank(paths=[str(data)], fuzzy=False)
        self.assertEqual(bank.load(), 1)
        self.assertTrue(any("答案为空" in issue for issue in bank.issues), bank.issues)

    def test_text_bank_whose_first_line_starts_with_bracket(self):
        """回归: 纯文本题库第一行常以 [单选题] 开头, 不能被误判成坏 JSON 而整份丢掉。"""
        text_bank = Path(self.tmp.name) / "bracket.txt"
        text_bank.write_text("[单选题]下列说法正确的是----A\n{判断题}HTTP是无状态的----正确\n",
                             encoding="utf-8")
        bank = LocalBank(paths=[str(text_bank)], fuzzy=False)
        self.assertEqual(bank.load(), 2)
        self.assertEqual(bank.issues, [])
        self.assertEqual(bank.lookup("[单选题]下列说法正确的是"), "A")

    def test_broken_json_bank_is_never_overwritten(self):
        """回归: 解析失败的题库文件绝不能被回写覆盖(否则用户原始题库就没了)。"""
        broken = Path(self.tmp.name) / "broken3.json"
        original = '{"下列哪一项属于网络层的协议？": "A", "HTTP 是无状态协议": "正确"'
        broken.write_text(original, encoding="utf-8")
        bank = LocalBank(paths=[str(broken)], fuzzy=False)
        self.assertEqual(bank.load(), 0)
        from api.tiku_bank import _path_key
        self.assertIn(_path_key(broken), bank.failed_paths)
        self.assertIsNone(bank.save())                       # 拒绝写入
        self.assertEqual(broken.read_text(encoding="utf-8"), original)  # 原文没被动过
        # 显式确认要覆盖时才写
        self.assertEqual(bank.save(allow_failed_overwrite=True), str(broken))

    def test_cache_can_correct_previously_exported_wrong_answer(self):
        """回归: 自动回写过的错答案必须还能被后来搜对的答案纠正(不能永久锁死)。"""
        bank_path = Path(self.tmp.name) / "auto.json"
        cache_path = Path(self.tmp.name) / "auto-cache.json"
        bank_path.write_text(json.dumps({"题目Q": {"answer": "A", "source": "cache:cache.json"}},
                                        ensure_ascii=False), encoding="utf-8")
        cache_path.write_text(json.dumps({"题目Q": "B"}, ensure_ascii=False), encoding="utf-8")
        bank = LocalBank(paths=[str(bank_path)], fuzzy=False)
        bank.load()
        result = bank.import_cache(cache_path)
        self.assertEqual(result["overwritten"], 1)
        self.assertEqual(bank.lookup("题目Q"), "B")

        # 人工核对过的答案(没有 source 或来源是人工)则保持不动
        curated = Path(self.tmp.name) / "curated.json"
        curated.write_text(json.dumps({"题目R": "A"}, ensure_ascii=False), encoding="utf-8")
        cache2 = Path(self.tmp.name) / "curated-cache.json"
        cache2.write_text(json.dumps({"题目R": "B"}, ensure_ascii=False), encoding="utf-8")
        bank2 = LocalBank(paths=[str(curated)], fuzzy=False)
        bank2.load()
        result2 = bank2.import_cache(cache2)
        self.assertEqual(result2["skipped"], 1)
        self.assertEqual(bank2.lookup("题目R"), "A")



class TestProvenanceAndSafety(unittest.TestCase):
    """第三轮验证报出的问题: 自动回写会覆盖人工修正 / 工具命令销毁题库 / 缓存格式与键不一致。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)
        self.bank_path = self.tmpdir / "bank.json"
        self.cache_path = self.tmpdir / "cache.json"

    def _load(self):
        bank = LocalBank(paths=[str(self.bank_path)], fuzzy=False, reload_interval=0)
        bank.load()
        return bank

    def test_auto_written_answer_can_be_corrected(self):
        """没人动过的自动答案(答案 == auto_answer) 允许被下一轮自动更新。"""
        self.bank_path.write_text(json.dumps(
            {"题目Q": {"answer": "A", "source": "cache:cache.json", "auto_answer": "A"}},
            ensure_ascii=False), encoding="utf-8")
        self.cache_path.write_text(json.dumps({"题目Q": "B"}, ensure_ascii=False), encoding="utf-8")
        bank = self._load()
        result = bank.import_cache(self.cache_path)
        self.assertEqual(result["overwritten"], 1)
        self.assertEqual(bank.lookup("题目Q"), "B")
        # 回写后的条目会记下这次自动写入的答案
        bank.save()
        data = json.loads(self.bank_path.read_text(encoding="utf-8"))
        self.assertEqual(data["题目Q"]["auto_answer"], "B")

    def test_hand_edited_answer_is_never_auto_reverted(self):
        """回归(high): 人工把答案改对之后, 自动回写不得再用旧缓存的错答案覆盖它。"""
        self.bank_path.write_text(json.dumps(
            {"题目Q": {"answer": "B", "source": "cache:cache.json", "auto_answer": "A"}},
            ensure_ascii=False), encoding="utf-8")
        self.cache_path.write_text(json.dumps({"题目Q": "A"}, ensure_ascii=False), encoding="utf-8")
        bank = self._load()
        result = bank.import_cache(self.cache_path)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["overwritten"], 0)
        self.assertEqual(bank.lookup("题目Q"), "B")

    def test_source_prefix_check_is_normalized(self):
        """回归(low): " Cache:x" / "CACHE:x" 不能被当成非缓存条目而受保护。"""
        self.assertTrue(LocalBank._can_overwrite({"answer": "A", "source": " Cache:x"}, "auto"))
        self.assertTrue(LocalBank._can_overwrite({"answer": "A", "source": "CACHE:x"}, "auto"))
        self.assertFalse(LocalBank._can_overwrite({"answer": "A", "source": " import"}, "auto"))

    def test_export_to_cache_uses_cache_key_format(self):
        """回归(medium): 同步缓存必须用 Tiku.query 读缓存时的同一个键(clean_title 处理后)。"""
        self.cache_path.write_text(json.dumps({"下列说法正确的是": "B"}, ensure_ascii=False),
                                   encoding="utf-8")
        self.bank_path.write_text(json.dumps({"（2.0分）下列说法正确的是": "A"}, ensure_ascii=False),
                                  encoding="utf-8")
        bank = self._load()
        written = bank.export_to_cache(self.cache_path)
        self.assertGreaterEqual(written, 1)
        cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertEqual(cached["下列说法正确的是"], "A")

    def test_export_to_cache_refuses_broken_cache(self):
        """回归(low): 损坏/非对象的缓存文件不能被整份覆盖。"""
        original = '{"半截": "A"'
        self.cache_path.write_text(original, encoding="utf-8")
        self.bank_path.write_text(json.dumps({"题": "A"}, ensure_ascii=False), encoding="utf-8")
        bank = self._load()
        self.assertEqual(bank.export_to_cache(self.cache_path), 0)
        self.assertIsNotNone(bank.last_refusal)
        self.assertEqual(self.cache_path.read_text(encoding="utf-8"), original)

    def test_export_to_cache_refuses_non_json_suffix(self):
        cache_txt = self.tmpdir / "cache.txt"
        cache_txt.write_text("{}", encoding="utf-8")
        self.bank_path.write_text(json.dumps({"题": "A"}, ensure_ascii=False), encoding="utf-8")
        bank = self._load()
        self.assertEqual(bank.export_to_cache(cache_txt), 0)
        self.assertIsNotNone(bank.last_refusal)

    def test_merge_does_not_destroy_existing_bank(self):
        """回归(medium): merge 命令必须把 --bank 里原有的题目也当作输入, 不能销毁。"""
        self.bank_path.write_text(json.dumps({"老题目": "A"}, ensure_ascii=False), encoding="utf-8")
        extra = self.tmpdir / "new.json"
        extra.write_text(json.dumps({"新题目": "B"}, ensure_ascii=False), encoding="utf-8")
        result = merge_bank_files([str(extra)], str(self.bank_path))
        self.assertEqual(result["entries"], 2)
        data = json.loads(self.bank_path.read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"老题目", "新题目"})

    def test_merge_refuses_non_json_output(self):
        text_bank = self.tmpdir / "myb.txt"
        text_bank.write_text("题目甲----A\n", encoding="utf-8")
        extra = self.tmpdir / "new2.json"
        extra.write_text(json.dumps({"新题目": "B"}, ensure_ascii=False), encoding="utf-8")
        result = merge_bank_files([str(extra)], str(text_bank))
        self.assertIsNone(result["output"])
        self.assertTrue(result["refusal"])

    def test_text_bank_first_line_with_quote(self):
        """回归(medium): 首行以 [ 开头且含引号的文本题库不能再被整份判成坏 JSON。"""
        text_bank = self.tmpdir / "quote.txt"
        text_bank.write_text('[判断题]What\'s this----正确\n[单选题]以下哪个是网络层协议----A\n',
                             encoding="utf-8")
        bank = LocalBank(paths=[str(text_bank)], fuzzy=False)
        self.assertEqual(bank.load(), 2)
        self.assertEqual(bank.lookup("[单选题]以下哪个是网络层协议"), "A")

    def test_truncated_json_in_txt_is_still_rejected(self):
        """反向保护: 截断的 JSON 放在 .txt 里也不能被当成题库(否则会把 JSON 片段当答案)。"""
        bad = self.tmpdir / "trunc.txt"
        bad.write_text('{"下列哪一项属于网络层的协议？": "A", "HTTP 是无状态协议": "正确"',
                       encoding="utf-8")
        bank = LocalBank(paths=[str(bad)], fuzzy=False)
        self.assertEqual(bank.load(), 0)
        self.assertTrue(any("json" in issue.lower() for issue in bank.issues), bank.issues)


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

# -*- coding: utf-8 -*-
"""scripts/tiku_tool.py 的测试: 题库导入/合并/回写/自检。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import tiku_tool  # noqa: E402


class TestParseTextEntries(unittest.TestCase):
    def test_various_separators(self):
        text = "\n".join([
            "题目一一一----A",
            "题目二二二$$对",
            "题目三三三\tBCD",
            "题目四四四,A",
            "题目五五五|答案内容|单选",
            "",
            "没有分隔符的行会被跳过",
            "# 注释行",
        ])
        entries, skipped = tiku_tool.parse_text_entries(text)
        mapping = {e["question"]: e["answer"] for e in entries}
        self.assertEqual(mapping["题目一一一"], "A")
        self.assertEqual(mapping["题目二二二"], "对")
        self.assertEqual(mapping["题目三三三"], "BCD")
        self.assertEqual(mapping["题目四四四"], "A")
        self.assertEqual(mapping["题目五五五"], "答案内容")
        self.assertEqual(entries[4]["type"], "single")
        self.assertEqual(skipped, 1)

    def test_empty(self):
        entries, skipped = tiku_tool.parse_text_entries("")
        self.assertEqual((entries, skipped), ([], 0))


class TestLoadInput(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)

    def test_json_input(self):
        path = self.tmpdir / "bank.json"
        path.write_text(json.dumps({"题干A": "A"}), encoding="utf-8")
        payload, skipped = tiku_tool.load_input(path)
        self.assertEqual(payload, {"题干A": "A"})
        self.assertEqual(skipped, 0)

    def test_text_input(self):
        path = self.tmpdir / "bank.txt"
        path.write_text("题干B----B\n", encoding="utf-8")
        payload, skipped = tiku_tool.load_input(path)
        self.assertEqual(payload[0]["question"], "题干B")

    def test_json_content_with_txt_extension(self):
        path = self.tmpdir / "bank.txt"
        path.write_text(json.dumps({"题干C": "C"}), encoding="utf-8")
        payload, _ = tiku_tool.load_input(path)
        self.assertEqual(payload, {"题干C": "C"})

    def test_missing_file(self):
        payload, skipped = tiku_tool.load_input(self.tmpdir / "nope.json")
        self.assertEqual((payload, skipped), ([], 0))


class TestCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)
        self.bank = self.tmpdir / "bank.json"
        self.cache = self.tmpdir / "cache.json"

    def test_import_stats_lookup_and_export(self):
        source = self.tmpdir / "source.txt"
        source.write_text("题目甲----A\n题目乙$$对\n", encoding="utf-8")

        code = tiku_tool.main(["import", "--bank", str(self.bank), "--input", str(source)])
        self.assertEqual(code, 0)
        data = json.loads(self.bank.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 2)

        code = tiku_tool.main(["stats", "--bank", str(self.bank), "--cache", str(self.cache)])
        self.assertEqual(code, 0)

        self.assertEqual(tiku_tool.main(["lookup", "--bank", str(self.bank), "-q", "题目甲"]), 0)
        self.assertEqual(tiku_tool.main(["lookup", "--bank", str(self.bank), "-q", "不存在的题目内容xyz"]), 1)

        self.cache.write_text(json.dumps({"题目丙": "C"}, ensure_ascii=False), encoding="utf-8")
        code = tiku_tool.main(["export", "--bank", str(self.bank), "--cache", str(self.cache)])
        self.assertEqual(code, 0)
        data = json.loads(self.bank.read_text(encoding="utf-8"))
        self.assertIn("题目丙", data)

    def test_dry_run_does_not_write(self):
        source = self.tmpdir / "source.txt"
        source.write_text("题目甲----A\n", encoding="utf-8")
        code = tiku_tool.main(["import", "--bank", str(self.bank), "--input", str(source), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertFalse(self.bank.exists())

    def test_merge_command(self):
        first = self.tmpdir / "a.json"
        second = self.tmpdir / "b.txt"
        first.write_text(json.dumps({"题干一": "A"}), encoding="utf-8")
        second.write_text("题干二----B\n", encoding="utf-8")
        code = tiku_tool.main(["merge", "--bank", str(self.bank), "--input", str(first), str(second)])
        self.assertEqual(code, 0)
        data = json.loads(self.bank.read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"题干一", "题干二"})

    def test_dedupe_normalizes(self):
        self.bank.write_text(json.dumps({"1、题干X？（2.0分）": "A", "题干X": "A"}), encoding="utf-8")
        code = tiku_tool.main(["dedupe", "--bank", str(self.bank)])
        self.assertEqual(code, 0)
        data = json.loads(self.bank.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 1)
        value = next(iter(data.values()))
        self.assertEqual(value["answer"] if isinstance(value, dict) else value, "A")

    def test_bank_flag_after_subcommand(self):
        # 文档里写的是 `stats --bank xxx`, 解析器必须支持这个顺序
        self.assertEqual(tiku_tool.main(["stats", "--bank", str(self.bank)]), 0)

    def test_bank_flag_before_subcommand(self):
        self.assertEqual(tiku_tool.main(["--bank", str(self.bank), "stats"]), 0)


if __name__ == "__main__":
    unittest.main()

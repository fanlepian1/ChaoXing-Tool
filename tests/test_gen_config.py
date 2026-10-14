# -*- coding: utf-8 -*-
"""scripts/gen_config.py 的测试: 从环境变量渲染 config.ini。"""

import configparser
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gen_config  # noqa: E402

TEMPLATE = """[common]
; 注释要保留
username = xxx
password = xxx
course_list = xxx
speed = 1
jobs = 4
notopen_action = retry

[tiku]
provider=TikuYanxi
submit=false

[notification]
provider=ServerChan
url=
"""


class TestRenderConfig(unittest.TestCase):
    def test_replaces_existing_key_and_keeps_comments(self):
        rendered = gen_config.render_config(TEMPLATE, {("common", "username"): "13800000000"})
        self.assertIn("; 注释要保留", rendered)
        self.assertIn("username = 13800000000", rendered)
        self.assertNotIn("username = xxx", rendered)

    def test_inserts_new_key_into_section(self):
        rendered = gen_config.render_config(TEMPLATE, {("tiku", "local_bank"): "tiku/bank.json"})
        sections = gen_config.split_sections(rendered)
        tiku = [s for s in sections if s["name"] == "tiku"][0]
        body = "\n".join(tiku["lines"])
        self.assertIn("local_bank = tiku/bank.json", body)
        # 新键必须落在 [tiku] 里, 不能串到别的节
        self.assertNotIn("local_bank", [s for s in sections if s["name"] == "common"][0]["lines"])

    def test_creates_missing_section(self):
        rendered = gen_config.render_config("[common]\nusername =\n", {("custom", "key"): "v"})
        self.assertIn("[custom]", rendered)
        self.assertIn("key = v", rendered)

    def test_rendered_output_is_valid_ini(self):
        values = {
            ("common", "username"): "u",
            ("common", "course_list"): "1,2",
            ("tiku", "provider"): "TikuLocal,TikuGo",
            ("tiku", "cache_file"): "tiku/cache.json",
            ("tiku", "submit"): "true",
            ("notification", "url"): "https://sctapi.ftqq.com/KEY.send",
        }
        rendered = gen_config.render_config(TEMPLATE, values)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.ini"
            path.write_text(rendered, encoding="utf-8")
            parser = configparser.ConfigParser()
            parser.read(path, encoding="utf-8")
        self.assertEqual(parser["common"]["username"], "u")
        self.assertEqual(parser["common"]["course_list"], "1,2")
        self.assertEqual(parser["tiku"]["provider"], "TikuLocal,TikuGo")
        self.assertEqual(parser["tiku"]["cache_file"], "tiku/cache.json")
        self.assertEqual(parser["notification"]["url"], "https://sctapi.ftqq.com/KEY.send")

    def test_empty_value_skips_existing_key(self):
        rendered = gen_config.render_config(TEMPLATE, {})
        self.assertIn("speed = 1", rendered)


class TestCollectValues(unittest.TestCase):
    def test_env_mapping(self):
        env = {
            "CHAOXING_USERNAME": "13800000000",
            "CHAOXING_PASSWORD": "pw",
            "CHAOXING_COURSE_LIST": "111,222",
            "CHAOXING_TIKU_PROVIDER": "auto",
            "CHAOXING_TIKU_TOKENS": "token1,token2",
            "CHAOXING_TIKU_SUBMIT": "true",
            "CHAOXING_TIKU_BANK": "tiku/bank.json",
            "CHAOXING_TIKU_CACHE": "tiku/cache.json",
            "CHAOXING_TIKU_EXPORT_BANK": "true",
            "CHAOXING_NOTIFY_URL": "https://sctapi.ftqq.com/K.send",
            "UNRELATED": "x",
        }
        values = gen_config.collect_values(env)
        self.assertEqual(values[("common", "username")], "13800000000")
        self.assertEqual(values[("common", "course_list")], "111,222")
        self.assertEqual(values[("tiku", "provider")], "auto")
        self.assertEqual(values[("tiku", "tokens")], "token1,token2")
        self.assertEqual(values[("tiku", "local_bank")], "tiku/bank.json")
        self.assertEqual(values[("tiku", "cache_file")], "tiku/cache.json")
        self.assertEqual(values[("tiku", "export_bank")], "true")
        self.assertEqual(values[("notification", "url")], "https://sctapi.ftqq.com/K.send")
        self.assertFalse([k for k in values if k[1] == "UNRELATED"])

    def test_empty_env_is_ignored(self):
        values = gen_config.collect_values({"CHAOXING_USERNAME": "   ", "CHAOXING_TIKU_TOKENS": ""})
        self.assertEqual(values, {})

    def test_mask_value(self):
        self.assertEqual(gen_config.mask_value("tokens", "abcdefgh"), "ab****gh")
        self.assertEqual(gen_config.mask_value("tokens", "abc"), "****")
        self.assertEqual(gen_config.mask_value("provider", "TikuLocal"), "TikuLocal")


class TestMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)
        self.template = self.tmpdir / "template.ini"
        self.template.write_text(TEMPLATE, encoding="utf-8")
        self.out = self.tmpdir / "config.ini"

    def test_writes_file_with_env_secrets(self):
        env = {"CHAOXING_USERNAME": "13800000000", "CHAOXING_PASSWORD": "pw",
               "CHAOXING_TIKU_LOCAL_BANK": "tiku/bank.json"}
        with mock.patch.dict("os.environ", env, clear=True):
            code = gen_config.main(["--template", str(self.template), "--out", str(self.out)])
        self.assertEqual(code, 0)
        parser = configparser.ConfigParser()
        parser.read(self.out, encoding="utf-8")
        self.assertEqual(parser["common"]["username"], "13800000000")
        self.assertEqual(parser["tiku"]["local_bank"], "tiku/bank.json")
        # 没有提供 provider 时给一个可用的默认值(否则题库根本不启用)
        self.assertEqual(parser["tiku"]["provider"], "auto")

    def test_default_provider_can_be_disabled(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            gen_config.main(["--template", str(self.template), "--out", str(self.out),
                             "--default-provider", ""])
        parser = configparser.ConfigParser()
        parser.read(self.out, encoding="utf-8")
        self.assertEqual(parser["tiku"]["provider"], "TikuYanxi")  # 保留模板里的原值

    def test_check_mode_does_not_write(self):
        with mock.patch.dict("os.environ", {"CHAOXING_USERNAME": "u"}, clear=True):
            code = gen_config.main(["--template", str(self.template), "--out", str(self.out), "--check"])
        self.assertEqual(code, 0)
        self.assertFalse(self.out.exists())

    def test_missing_template_falls_back_to_builtin(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            gen_config.main(["--template", str(self.tmpdir / "nope.ini"), "--out", str(self.out)])
        parser = configparser.ConfigParser()
        parser.read(self.out, encoding="utf-8")
        self.assertIn("tiku", parser.sections())

    def test_template_placeholders_are_cleared(self):
        """模板里的 xxx 占位值绝不能进最终配置(否则会拿 XXX 去登录 / 把 xxx 当课程ID)。"""
        with mock.patch.dict("os.environ", {}, clear=True):
            gen_config.main(["--template", str(self.template), "--out", str(self.out)])
        parser = configparser.ConfigParser()
        parser.read(self.out, encoding="utf-8")
        self.assertEqual(parser["common"]["username"], "")
        self.assertEqual(parser["common"]["password"], "")
        self.assertEqual(parser["common"]["course_list"], "")

    def test_env_file_is_loaded(self):
        env_file = self.tmpdir / ".env.local"
        env_file.write_text("CHAOXING_TIKU_TOKENS=from-file\n# 注释\nCHAOXING_JOBS=2\n", encoding="utf-8")
        with mock.patch.dict("os.environ", {}, clear=True):
            gen_config.main(["--template", str(self.template), "--out", str(self.out),
                             "--env-file", str(env_file)])
        parser = configparser.ConfigParser()
        parser.read(self.out, encoding="utf-8")
        self.assertEqual(parser["tiku"]["tokens"], "from-file")
        self.assertEqual(parser["common"]["jobs"], "2")


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""端到端集成测试: 复刻 GitHub Actions 里的配置路径。

Actions 里的顺序是:
    1. gen_config.py 用环境变量(secrets)渲染 config.ini
    2. main.py --config config.ini 读取配置
    3. TikuLocal 从 tiku/bank.json 命中答案, 并把答案写进 tiku/cache.json
这个测试就是把这四步原样跑一遍, 任何一步断链都会暴露出来。
"""

import configparser
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import gen_config  # noqa: E402
import main as main_module  # noqa: E402
from api.answer import CacheDAO, Tiku, TikuLocal  # noqa: E402

TEMPLATE = REPO_ROOT / "config_template.ini"


class TestWorkflowConfigPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)
        self.config_path = self.tmpdir / "config.ini"
        self._old_cwd = os.getcwd()
        self._old_cache_default = CacheDAO.DEFAULT_CACHE_FILE
        self.addCleanup(self._restore)

    def _restore(self):
        os.chdir(self._old_cwd)
        CacheDAO.DEFAULT_CACHE_FILE = self._old_cache_default

    def _write_bank(self):
        bank_dir = self.tmpdir / "tiku"
        bank_dir.mkdir(parents=True, exist_ok=True)
        (bank_dir / "bank.json").write_text(
            json.dumps({"1、以下哪个是网络层协议？（2.0分）": "A"}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_render_then_boot_tiku_local(self):
        self._write_bank()
        env = {
            "CHAOXING_USERNAME": "13800000000",
            "CHAOXING_PASSWORD": "pw",
            "CHAOXING_TIKU_PROVIDER": "auto",
            # 不提供任何第三方凭据: auto 只应该组出离线题库
        }
        with mock.patch.dict(os.environ, env, clear=True):
            code = gen_config.main(["--template", str(TEMPLATE), "--out", str(self.config_path)])
            self.assertEqual(code, 0)

            parser = configparser.ConfigParser()
            parser.read(self.config_path, encoding="utf-8")
            self.assertEqual(parser["tiku"]["provider"], "auto")
            self.assertEqual(parser["tiku"]["local_bank"], "tiku/bank.json")
            self.assertEqual(parser["tiku"]["cache_file"], "tiku/cache.json")

            os.chdir(self.tmpdir)  # 相对路径以仓库根(这里的临时目录)为基准

            common, tiku_config, notify = main_module.load_config_from_file(str(self.config_path))
            merged = main_module.resolve_tiku_config(tiku_config, None, os.environ)
            self.assertEqual(merged["provider"], "TikuLocal,TikuGo")

            base = Tiku()
            base.config_set(merged)
            tiku = base.get_tiku_from_config()
            tiku.init_tiku()

        self.assertFalse(tiku.DISABLE)
        # auto 会组出 TikuLocal,TikuGo, 所以拿到的是回退链; 链首必须是离线题库
        self.assertIsInstance(tiku.providers[0], TikuLocal)
        answer = tiku.query({"title": "1、以下哪个是网络层协议？（2.0分）", "type": "single",
                             "options": "A. IP\nB. TCP"})
        self.assertEqual(answer, "IP")  # 题库里的字母 A 会按当前选项展开成选项文本

        cache_file = self.tmpdir / "tiku" / "cache.json"
        self.assertTrue(cache_file.is_file(), "答案应该写进 tiku/cache.json")
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        self.assertEqual(list(cached.values()), ["IP"])

    def test_auto_chain_includes_configured_providers(self):
        self._write_bank()
        env = {
            "CHAOXING_USERNAME": "13800000000",
            "CHAOXING_PASSWORD": "pw",
            "CHAOXING_TIKU_TOKENS": "token-a",
            "CHAOXING_TIKU_URL": "http://127.0.0.1:9000/query",
            "CHAOXING_TIKU_SILICONFLOW_KEY": "sk-sf",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            gen_config.main(["--template", str(TEMPLATE), "--out", str(self.config_path)])
            os.chdir(self.tmpdir)
            _, tiku_config, _ = main_module.load_config_from_file(str(self.config_path))
            merged = main_module.resolve_tiku_config(tiku_config, None, os.environ)

        chain = merged["provider"].split(",")
        self.assertEqual(chain[0], "TikuLocal")
        self.assertIn("TikuYanxi", chain)
        self.assertIn("TikuAdapter", chain)
        self.assertIn("SiliconFlow", chain)
        # 回退链顺序: 离线题库优先, 付费/AI 题库垫后
        self.assertLess(chain.index("TikuLocal"), chain.index("TikuYanxi"))
        self.assertLess(chain.index("TikuAdapter"), chain.index("SiliconFlow"))

    def test_submit_flag_from_env_reaches_tiku(self):
        self._write_bank()
        env = {
            "CHAOXING_USERNAME": "u",
            "CHAOXING_PASSWORD": "p",
            "CHAOXING_TIKU_PROVIDER": "TikuLocal",
            "CHAOXING_TIKU_SUBMIT": "true",
            "CHAOXING_TIKU_COVER_RATE": "0.5",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            gen_config.main(["--template", str(TEMPLATE), "--out", str(self.config_path)])
            os.chdir(self.tmpdir)
            _, tiku_config, _ = main_module.load_config_from_file(str(self.config_path))
            merged = main_module.resolve_tiku_config(tiku_config, None, os.environ)
            base = Tiku()
            base.config_set(merged)
            tiku = base.get_tiku_from_config()
            tiku.init_tiku()

        self.assertTrue(tiku.SUBMIT)
        self.assertEqual(tiku.COVER_RATE, 0.5)
        self.assertEqual(tiku.get_submit_params(), "")  # 空串=直接提交

    def test_export_bank_after_run(self):
        self._write_bank()
        env = {
            "CHAOXING_USERNAME": "u",
            "CHAOXING_PASSWORD": "p",
            "CHAOXING_TIKU_PROVIDER": "TikuLocal",
            "CHAOXING_TIKU_EXPORT_BANK": "true",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            gen_config.main(["--template", str(TEMPLATE), "--out", str(self.config_path)])
            os.chdir(self.tmpdir)
            (self.tmpdir / "tiku" / "cache.json").write_text(
                json.dumps({"新攒到的题目": "B"}, ensure_ascii=False), encoding="utf-8")
            _, tiku_config, _ = main_module.load_config_from_file(str(self.config_path))
            merged = main_module.resolve_tiku_config(tiku_config, None, os.environ)
            main_module.export_tiku_bank(merged)

        data = json.loads((self.tmpdir / "tiku" / "bank.json").read_text(encoding="utf-8"))
        self.assertIn("新攒到的题目", data)
        self.assertIn("1、以下哪个是网络层协议？（2.0分）", data)


if __name__ == "__main__":
    unittest.main()

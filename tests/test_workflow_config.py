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

        # 本地题库命中不会写缓存(缓存只积累外部题库搜到的答案),
        # workflow 里的回写步骤对"缓存文件不存在"是有保护的
        cache_file = self.tmpdir / "tiku" / "cache.json"
        self.assertEqual(json.loads(cache_file.read_text(encoding="utf-8")), {})

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


class TestWorkflowFile(unittest.TestCase):
    """workflow 的静态自检(不依赖 pyyaml, 只做文本断言, 但能挡住最常见的改坏方式)。"""

    def setUp(self):
        self.path = REPO_ROOT / ".github" / "workflows" / "main.yml"
        self.text = self.path.read_text(encoding="utf-8")

    def test_referenced_files_exist(self):
        for rel in ("scripts/gen_config.py", "scripts/tiku_tool.py", "main.py",
                    "config_template.ini", "requirements.txt", "tiku/bank.json"):
            self.assertTrue((REPO_ROOT / rel).is_file(), f"{rel} 不存在")
            self.assertIn(rel, self.text, f"workflow 引用的 {rel} 不在文件里")

    def test_cannot_retrigger_itself(self):
        # 题库回写会 push, 必须同时满足: 提交信息带 [skip ci] + push 事件忽略 tiku/**
        self.assertIn("[skip ci]", self.text)
        self.assertIn("paths-ignore", self.text)
        self.assertIn("'tiku/**'", self.text)

    def test_cache_save_is_guarded(self):
        # actions/cache/save 在文件不存在时会直接失败, 所以回写步骤必须先保证文件存在
        self.assertIn("mkdir -p tiku", self.text)
        self.assertIn("tiku/cache.json", self.text)

    def test_secrets_are_not_hardcoded(self):
        """凭据只能来自 secrets 或由 secrets 注入的环境变量, 不能写死。"""
        for line in self.text.splitlines():
            if not any(key in line for key in ("CHAOXING_PASSWORD", "CHAOXING_USERNAME", "TIKU_TOKENS", "TIKU_KEY")):
                continue
            from_secrets = "secrets." in line
            from_env = '"$' in line or "${" in line  # shell 展开上一节由 secrets 注入的环境变量
            self.assertTrue(from_secrets or from_env, f"疑似明文凭据: {line.strip()}")

    def test_manual_trigger_is_wired(self):
        """手动触发必须真的可用: 每个输入都要被引用, 否则界面上填了没反应。"""
        self.assertIn("workflow_dispatch", self.text)
        for key in ("course_list", "provider", "submit", "speed", "jobs", "bank_url", "dry_run"):
            self.assertIn(f"github.event.inputs.{key}", self.text, f"{key} 没有接到任何地方")
        # 干跑与导库都要有对应的条件分支
        self.assertIn("github.event.inputs.dry_run == 'true'", self.text)
        self.assertIn("github.event.inputs.dry_run != 'true'", self.text)
        self.assertIn("github.event.inputs.bank_url != ''", self.text)

    def test_run_summary_is_emitted(self):
        """运行摘要在 Actions 页面直接显示命中情况, 不该被顺手删掉。"""
        self.assertIn("GITHUB_STEP_SUMMARY", self.text)
        self.assertIn("章节检测题库覆盖率", self.text)


    def test_bank_writeback_runs_even_on_failure(self):
        # 中途失败也要把这一轮搜到的答案落盘, 不能白跑
        self.assertIn("if: always()", self.text)


if __name__ == "__main__":
    unittest.main()

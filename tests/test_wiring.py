"""接线守护: 走真实主流程的取链路径(Tiku -> get_tiku_from_config -> init_tiku)。

为什么需要这个文件: 题库 provider 的配置是在 init_tiku() 里才生效的(config_set 只存配置)。
单测如果都自己 new 一个 provider 再手动 init, 就会漏掉"主流程有没有真的调用 init_tiku"这类问题;
工作流里曾经因为漏掉它, 本地题库 92 条一条都没参与匹配(GO题在兜底, 所以表面看不出来)。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api.answer import Tiku  # noqa: E402


class TestRealWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)
        self.bank = self.tmpdir / "bank.json"
        self.bank.write_text(json.dumps({
            "中国的首都是( )。": "北京",
            "1、以下哪个是网络层协议？(2.0分)": "IP",
        }, ensure_ascii=False), encoding="utf-8")
        self.cache = self.tmpdir / "cache.json"

    def _conf(self, provider):
        return {
            "provider": provider,
            "local_bank": str(self.bank),
            "cache_file": str(self.cache),
            "submit": "false",
        }

    def _build(self, provider):
        tiku = Tiku()
        tiku.config_set(self._conf(provider))
        chain = tiku.get_tiku_from_config()
        chain.init_tiku()          # main.py:464 就是这么调用的
        return chain

    def test_single_provider_loads_bank_and_hits(self):
        chain = self._build("TikuLocal")
        self.assertTrue(chain.name.startswith("本地"))
        self.assertIsNotNone(chain.bank)
        self.assertEqual(len(chain.bank), 2)
        self.assertEqual(chain.query({"title": "中国的首都是（　）。", "type": "single"}), "北京")
        self.assertEqual(chain.query({"title": "以下哪个是网络层协议？", "type": "single"}), "IP")

    def test_fallback_initializes_every_provider(self):
        """多题库链路里, 每个子题库都必须被 init(否则本地题库是死的, 只有网络题库在答)。"""
        chain = self._build("TikuLocal,TikuGo")
        names = [getattr(p, "name", type(p).__name__) for p in chain.providers]
        self.assertEqual(len(names), 2, names)
        local = [p for p in chain.providers if p.name.startswith("本地")][0]
        self.assertIsNotNone(local.bank, "链路里的本地题库没有被初始化")
        self.assertEqual(len(local.bank), 2)
        # 本地命中时不会碰网络题库
        self.assertEqual(chain.query({"title": "中国的首都是( )。", "type": "single"}), "北京")

    def test_without_init_bank_is_inert_not_wrong(self):
        """没 init 时不该抛异常、也不该给出错答案(只是没命中)。"""
        tiku = Tiku()
        tiku.config_set(self._conf("TikuLocal"))
        chain = tiku.get_tiku_from_config()
        self.assertIsNone(chain.query({"title": "中国的首都是( )。", "type": "single"}))

    def test_submit_and_cover_rate_come_from_conf(self):
        chain = self._build("TikuLocal")
        self.assertFalse(chain.SUBMIT)
        self.assertAlmostEqual(chain.COVER_RATE, 0.9)
        conf = self._conf("TikuLocal")
        conf["submit"] = "true"
        conf["cover_rate"] = "0.75"
        tiku = Tiku()
        tiku.config_set(conf)
        chain2 = tiku.get_tiku_from_config()
        chain2.init_tiku()
        self.assertTrue(chain2.SUBMIT)
        self.assertAlmostEqual(chain2.COVER_RATE, 0.75)
        self.assertEqual(os.path.normpath(str(self.cache)),
                         os.path.normpath(__import__("api.answer", fromlist=["CacheDAO"]).CacheDAO.DEFAULT_CACHE_FILE))


if __name__ == "__main__":
    unittest.main()

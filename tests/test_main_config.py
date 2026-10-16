# -*- coding: utf-8 -*-
"""main.py 配置层的测试: 优先级、类型归整、题库默认值兜底、非交互保护。"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as main_module  # noqa: E402
from api.exceptions import InputFormatError  # noqa: E402
from api.answer import Tiku  # noqa: E402


class TestMaskProofCourseIds(unittest.TestCase):
    """课程ID 恰好等于某个 secret 时会 GitHuc 被打码 —— 必须还能读出来。"""

    def test_mask_proof_splits_digits(self):
        self.assertEqual(main_module.mask_proof("268053034"), "268 053 034")
        self.assertEqual(main_module.mask_proof("123456789012"), "123 456 789 012")

    def test_short_or_non_digit_kept(self):
        self.assertEqual(main_module.mask_proof("12345"), "12345")
        self.assertEqual(main_module.mask_proof("abc-123"), "abc-123")
        self.assertEqual(main_module.mask_proof(None), "None")

    def test_describe_courses_includes_both_forms(self):
        text = main_module.describe_courses(
            [{"courseId": "268053034", "classId": None, "title": "中国共产党历史"}])
        self.assertIn("268 053 034", text, "必须有防打码写法")
        self.assertIn("中国共产党历史", text)


class TestListCoursesWiring(unittest.TestCase):
    """--list-courses 必须真的生效, 而且不能引用 main() 里不存在的变量。

    教训: 上一版把开关写在 main() 里却引用了 args(它只在 init_config() 里), CI 直接 NameError。
    """

    def test_flag_lands_in_common_config(self):
        """用真实解析器跑一遍: --list-courses 必须落进 common_config。"""
        with mock.patch.object(sys, "argv", ["main.py", "--list-courses"]):
            common_config, _, _ = main_module.init_config()
        self.assertTrue(common_config.get("list_courses"))

    def test_main_prints_course_table_and_stops(self):
        courses = [{"courseId": "268053034", "classId": "999", "title": "中国共产党历史"}]
        fake_chaoxing = mock.Mock()
        fake_chaoxing.login.return_value = {"status": True, "msg": "ok"}
        fake_chaoxing.get_course_list.return_value = courses

        with mock.patch.object(main_module, "init_config",
                              return_value=({"username": "u", "password": "p",
                                             "list_courses": True}, {}, {})), \
                mock.patch.object(main_module, "init_chaoxing", return_value=fake_chaoxing), \
                mock.patch.object(main_module, "Notification"), \
                mock.patch.object(main_module, "configure_console_logger"), \
                mock.patch.object(main_module, "export_tiku_bank"), \
                mock.patch.object(main_module, "process_course") as fake_process, \
                mock.patch.object(main_module, "logger") as fake_logger:
            main_module.main()

        self.assertFalse(fake_process.called, "只列课程时不该开始刷课")
        logged = " ".join(str(call) for call in fake_logger.info.call_args_list)
        self.assertIn("268053034", logged, "必须把课程表打出来(courseId 要能找到)")
        self.assertIn("中国共产党历史", logged)


class TestCourseFilter(unittest.TestCase):
    """课程ID 匹配: 常见的"填错"都要能容错或说清楚原因。"""

    COURSES = [
        {"courseId": "2151141", "classId": "987654", "title": "中国共产党历史"},
        {"courseId": "189191", "classId": "111222", "title": "大学英语"},
    ]

    def test_plain_ids(self):
        got = main_module.filter_courses(self.COURSES, ["2151141"])
        self.assertEqual([c["title"] for c in got], ["中国共产党历史"])

    def test_fullwidth_comma_and_newlines(self):
        got = main_module.filter_courses(self.COURSES, "2151141，\n189191\n")
        self.assertEqual(len(got), 2)

    def test_spaces_and_extra_commas(self):
        got = main_module.filter_courses(self.COURSES, " 2151141 , , 189191 ")
        self.assertEqual(len(got), 2)

    def test_class_id_also_matches(self):
        """很多人从网址里复制的是班级号(classId), 也应该能匹配上。"""
        got = main_module.filter_courses(self.COURSES, ["987654"])
        self.assertEqual([c["title"] for c in got], ["中国共产党历史"])

    def test_int_input(self):
        got = main_module.filter_courses(self.COURSES, [2151141])
        self.assertEqual(len(got), 1)

    def test_not_found_error_lists_available_courses(self):
        with self.assertRaises(InputFormatError) as ctx:
            main_module.filter_courses(self.COURSES, ["000000"])
        message = str(ctx.exception)
        self.assertIn("courseId", message)

    def test_describe_courses_contains_ids(self):
        text = main_module.describe_courses(self.COURSES)
        self.assertIn("2151141", text)
        self.assertIn("987654", text)
        self.assertIn("中国共产党历史", text)

    def test_empty_course_list_uses_all(self):
        """CI/非交互环境下, 不填课程ID就应该刷全部课程(不能卡在 input())。

        注意: 这个行为取决于 stdin 是否可交互, 测试里必须把它固定成"非交互", 否则在本地
        连着控制台跑测试时会走到 input() 分支(这正是之前 CI 与本地表现不一致的老坑)。
        """
        with mock.patch.object(main_module, "stdin_is_interactive", return_value=False):
            self.assertEqual(len(main_module.filter_courses(self.COURSES, [])), 2)


class TestResolveTikuConfig(unittest.TestCase):
    def test_defaults_never_enable_tiku(self):
        conf = main_module.resolve_tiku_config({}, None, {})
        self.assertEqual(conf["provider"], "")
        self.assertEqual(conf["local_bank"], "tiku/bank.json")
        self.assertEqual(conf["cache_file"], "tiku/cache.json")
        self.assertEqual(conf["submit"], "false")

    def test_priority_file_env_args(self):
        file_conf = {"provider": "TikuLocal", "submit": "false", "tokens": "file-token"}
        env = {"CHAOXING_TIKU_SUBMIT": "true", "CHAOXING_TIKU_TOKENS": "env-token"}
        fake_args = type("A", (), {
            "tiku_provider": None, "tiku_tokens": "cli-token", "tiku_url": None, "tiku_endpoint": None,
            "tiku_key": None, "tiku_model": None, "tiku_go_authorization": None, "tiku_siliconflow_key": None,
            "tiku_local_bank": None, "tiku_local_api": None, "tiku_cache_file": None, "tiku_submit": None,
            "tiku_cover_rate": None, "tiku_delay": None, "tiku_export_bank": False, "tiku_opt": None,
            "no_tiku": False,
        })()
        conf = main_module.resolve_tiku_config(file_conf, fake_args, env)
        self.assertEqual(conf["tokens"], "cli-token")   # 命令行最高
        self.assertEqual(conf["submit"], "true")        # 环境变量覆盖文件
        self.assertEqual(conf["provider"], "TikuLocal")

    def test_auto_provider_expansion(self):
        conf = main_module.resolve_tiku_config({"provider": "auto"}, None,
                                              {"CHAOXING_TIKU_TOKENS": "t",
                                               "CHAOXING_TIKU_ENDPOINT": "https://x/v1",
                                               "CHAOXING_TIKU_KEY": "sk"})
        chain = conf["provider"].split(",")
        self.assertEqual(chain[0], "TikuLocal")
        self.assertIn("TikuYanxi", chain)
        self.assertIn("AI", chain)
        self.assertNotIn("TikuAdapter", chain)  # 没配 url 就不该出现

    def test_auto_without_credentials(self):
        """零凭据时的默认链: 离线题库 + 免费公共题库 TikuGo。"""
        conf = main_module.resolve_tiku_config({"provider": "auto"}, None, {})
        self.assertEqual(conf["provider"], "TikuLocal,TikuGo")

    def test_auto_can_exclude_public_tiku_go(self):
        """不希望题目离开本机时, 可以只用离线题库。"""
        conf = main_module.resolve_tiku_config({"provider": "auto"}, None,
                                              {"CHAOXING_TIKU_GO_ENABLED": "false"})
        self.assertEqual(conf["provider"], "TikuLocal")
        self.assertNotIn("TikuGo", conf["provider"])

    def test_no_tiku_flag_disables(self):
        fake_args = type("A", (), {
            "tiku_provider": None, "tiku_tokens": None, "tiku_url": None, "tiku_endpoint": None,
            "tiku_key": None, "tiku_model": None, "tiku_go_authorization": None, "tiku_siliconflow_key": None,
            "tiku_local_bank": None, "tiku_local_api": None, "tiku_cache_file": None, "tiku_submit": None,
            "tiku_cover_rate": None, "tiku_delay": None, "tiku_export_bank": False, "tiku_opt": None,
            "no_tiku": True,
        })()
        conf = main_module.resolve_tiku_config({"provider": "TikuLocal"}, fake_args, {})
        self.assertEqual(conf["provider"], "")

    def test_numeric_keys_are_coerced(self):
        conf = main_module.resolve_tiku_config({"delay": "1.5", "cover_rate": "0.5", "local_threshold": "0.9"},
                                               None, {})
        self.assertIsInstance(conf["delay"], float)
        self.assertEqual(conf["delay"], 1.5)
        self.assertEqual(conf["cover_rate"], 0.5)
        self.assertEqual(conf["local_threshold"], 0.9)

    def test_invalid_numeric_is_dropped(self):
        conf = main_module.resolve_tiku_config({"delay": "abc"}, None, {})
        self.assertEqual(conf["delay"], 1.0)  # 回落到默认值, 而不是把字符串塞给 time.sleep

    def test_empty_file_values_do_not_override_defaults(self):
        """配置模板里有大量空值(如 cache_file=), 它们不能把默认值冲掉。"""
        conf = main_module.resolve_tiku_config({"cache_file": "", "local_bank": "  ",
                                                "provider": "TikuLocal", "local_api": ""}, None, {})
        self.assertEqual(conf["cache_file"], "tiku/cache.json")
        self.assertEqual(conf["local_bank"], "tiku/bank.json")
        self.assertEqual(conf["provider"], "TikuLocal")

    def test_tiku_opt_overrides(self):
        fake_args = type("A", (), {
            "tiku_provider": None, "tiku_tokens": None, "tiku_url": None, "tiku_endpoint": None,
            "tiku_key": None, "tiku_model": None, "tiku_go_authorization": None, "tiku_siliconflow_key": None,
            "tiku_local_bank": None, "tiku_local_api": None, "tiku_cache_file": None, "tiku_submit": None,
            "tiku_cover_rate": None, "tiku_delay": None, "tiku_export_bank": True, "tiku_opt": ["submit=true"],
            "no_tiku": False,
        })()
        conf = main_module.resolve_tiku_config({}, fake_args, {})
        self.assertEqual(conf["submit"], "true")
        self.assertEqual(conf["export_bank"], "true")


class TestBuildCommonConfig(unittest.TestCase):
    def _args(self, **kw):
        base = dict(username=None, password=None, list=None, speed=None, jobs=None,
                    notopen_action=None, use_cookies=False, verbose=False)
        base.update(kw)
        return type("A", (), base)()

    def test_file_speed_survives_missing_cli_arg(self):
        conf = main_module.build_common_config(self._args(), {"speed": 2, "jobs": 8, "username": "u", "password": "p"})
        self.assertEqual(conf["speed"], 2.0)
        self.assertEqual(conf["jobs"], 8)

    def test_env_overrides_file_but_not_cli(self):
        with mock.patch.dict(os.environ, {"CHAOXING_COURSE_LIST": "111,222", "CHAOXING_SPEED": "1.5"}):
            conf = main_module.build_common_config(self._args(), {"course_list": ["999"], "speed": 2})
        self.assertEqual(conf["course_list"], ["111", "222"])
        self.assertEqual(conf["speed"], 1.5)

        conf = main_module.build_common_config(self._args(list="333", speed=2.0), {"course_list": ["999"]})
        self.assertEqual(conf["course_list"], ["333"])
        self.assertEqual(conf["speed"], 2.0)

    def test_defaults_and_clamping(self):
        conf = main_module.build_common_config(self._args(), {})
        self.assertEqual(conf["speed"], 1.0)
        self.assertEqual(conf["jobs"], 4)
        self.assertEqual(conf["notopen_action"], "retry")
        self.assertFalse(conf["use_cookies"])

    def test_speed_is_clamped_to_1_2(self):
        self.assertEqual(main_module.build_common_config(self._args(speed=9), {})["speed"], 2.0)
        self.assertEqual(main_module.build_common_config(self._args(speed=0.1), {})["speed"], 1.0)


class TestCliParsing(unittest.TestCase):
    def _parse(self, argv):
        with mock.patch.object(sys, "argv", ["main.py"] + argv):
            return main_module.parse_args()

    def test_tiku_flags(self):
        args = self._parse(["-u", "u", "-p", "p", "--tiku-provider", "TikuLocal,TikuGo",
                            "--tiku-local-bank", "tiku/bank.json", "--tiku-submit", "false",
                            "--tiku-cover-rate", "0.5", "--tiku-opt", "local_threshold=0.7"])
        self.assertEqual(args.tiku_provider, "TikuLocal,TikuGo")
        self.assertFalse(args.tiku_submit)
        self.assertEqual(args.tiku_cover_rate, 0.5)
        self.assertEqual(args.tiku_opt, ["local_threshold=0.7"])

    def test_bare_submit_flag_means_true(self):
        self.assertTrue(self._parse(["--tiku-submit"]).tiku_submit)

    def test_defaults_are_none_for_merge(self):
        args = self._parse([])
        self.assertIsNone(args.speed)
        self.assertIsNone(args.jobs)
        self.assertIsNone(args.notopen_action)
        self.assertIsNone(args.tiku_provider)


class TestConfigFileIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)

    def test_partial_tiku_section_does_not_crash(self):
        """回归: [tiku] 少写 submit/cover_rate/true_list/false_list 时旧代码会 KeyError。"""
        ini = self.tmpdir / "config.ini"
        ini.write_text(
            "[common]\nusername = u\npassword = p\ncourse_list = 1\n\n"
            "[tiku]\nprovider = TikuLocal\nlocal_bank = tiku/bank.json\n",
            encoding="utf-8",
        )
        common, tiku_config, notify = main_module.load_config_from_file(str(ini))
        merged = main_module.resolve_tiku_config(tiku_config, self._args(), {})
        base = Tiku()
        base.config_set(merged)
        tiku = base.get_tiku_from_config()
        tiku.init_tiku()  # 不应该抛异常
        self.assertFalse(tiku.DISABLE)
        self.assertEqual(tiku.COVER_RATE, 0.9)
        self.assertEqual(common["username"], "u")

    def _args(self):
        return type("A", (), {
            "tiku_provider": None, "tiku_tokens": None, "tiku_url": None, "tiku_endpoint": None,
            "tiku_key": None, "tiku_model": None, "tiku_go_authorization": None, "tiku_siliconflow_key": None,
            "tiku_local_bank": None, "tiku_local_api": None, "tiku_cache_file": None, "tiku_submit": None,
            "tiku_cover_rate": None, "tiku_delay": None, "tiku_export_bank": False, "tiku_opt": None,
            "no_tiku": False,
        })()

    def test_init_config_merges_file_env_and_defaults(self):
        """走真实的 init_config(): 文件空值不覆盖默认值, 环境变量优先级更高。"""
        ini = self.tmpdir / "config.ini"
        ini.write_text("[common]\nusername = u\npassword = p\ncourse_list =\n\n"
                       "[tiku]\nprovider = TikuLocal\ncache_file =\nlocal_bank =\n\n"
                       "[notification]\nprovider = ServerChan\nurl =\n",
                       encoding="utf-8")
        env = {"CHAOXING_NOTIFY_URL": "https://sctapi.ftqq.com/K.send",
               "CHAOXING_JOBS": "6"}
        with mock.patch.object(sys, "argv", ["main.py", "--config", str(ini)]):
            with mock.patch.dict(os.environ, env, clear=True):
                common, tiku_config, notify = main_module.init_config()
        self.assertEqual(notify["url"], "https://sctapi.ftqq.com/K.send")
        self.assertEqual(notify["provider"], "ServerChan")
        self.assertEqual(tiku_config["cache_file"], "tiku/cache.json")   # 默认值没被空值冲掉
        self.assertEqual(tiku_config["local_bank"], "tiku/bank.json")
        self.assertEqual(tiku_config["provider"], "TikuLocal")
        self.assertEqual(common["jobs"], 6)                              # 环境变量覆盖文件默认
        self.assertEqual(common["username"], "u")

    def test_export_tiku_bank_merges_cache(self):
        bank = self.tmpdir / "bank.json"
        cache = self.tmpdir / "cache.json"
        bank.write_text(json.dumps({"老题目": "A"}, ensure_ascii=False), encoding="utf-8")
        cache.write_text(json.dumps({"新题目": "B"}, ensure_ascii=False), encoding="utf-8")
        main_module.export_tiku_bank({"export_bank": "true", "local_bank": str(bank), "cache_file": str(cache)})
        data = json.loads(bank.read_text(encoding="utf-8"))
        self.assertIn("老题目", data)
        self.assertIn("新题目", data)

    def test_export_disabled_by_default(self):
        bank = self.tmpdir / "bank.json"
        cache = self.tmpdir / "cache.json"
        bank.write_text("{}", encoding="utf-8")
        cache.write_text(json.dumps({"新题目": "B"}, ensure_ascii=False), encoding="utf-8")
        main_module.export_tiku_bank({"local_bank": str(bank), "cache_file": str(cache)})
        self.assertEqual(json.loads(bank.read_text(encoding="utf-8")), {})

    def test_describe_tiku_config_masks_secrets(self):
        text = main_module.describe_tiku_config({"provider": "TikuLocal", "tokens": "supersecrettoken",
                                                 "key": "sk-abcdefg", "empty": ""})
        self.assertIn("provider=TikuLocal", text)
        self.assertNotIn("supersecrettoken", text)
        self.assertNotIn("sk-abcdefg", text)
        self.assertNotIn("empty", text)


if __name__ == "__main__":
    unittest.main()

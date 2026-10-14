# -*- coding: utf-8 -*-
"""api/base.py 选项匹配的测试。

背景(独立验证报出的错误答案场景):
    题库返回"选项原文"或由字母展开而来的答案时, base.py 原先只会"按顺序找第一个把答案当子序列的选项",
    遇到嵌套文本会命中错误的选项:
        选项 A. 操作系统是一种系统软件 / B. 图形界面 / C. 系统软件, 答案是 C
        -> "系统软件" 是 A 的子序列 -> 填了 A(错)
    现在改为先做"选项正文精确相等"匹配, 匹配不到再走子序列/相似度兜底。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.base import (  # noqa: E402
    best_option_by_similarity,
    exact_option_letter,
    get_option_text,
    is_subsequence,
    normalize_text,
)

NESTED_OPTIONS = ["A. 操作系统是一种系统软件", "B. 图形界面", "C. 系统软件"]
ENGLISH_OPTIONS = ["A. CPU 是中央处理器", "B. 内存是存储设备", "C. 硬盘是外存"]


def simulate_single_branch(answer_text: str, options: list) -> str:
    """复刻 api/base.py 里 single 分支的匹配顺序, 用于验证最终填哪个字母。"""
    t_res = [answer_text.strip()]
    letter = exact_option_letter(t_res[0], options)
    if letter:
        return letter
    for option in options:
        if is_subsequence(t_res[0], option):
            return option[:1]
    return best_option_by_similarity(t_res[0], options, threshold=0.8)


def simulate_multiple_branch(parts: list, options: list) -> str:
    answer = ""
    for part in parts:
        letter = exact_option_letter(part, options)
        if letter:
            answer += letter
            continue
        matched = False
        for option in options:
            if is_subsequence(part, option):
                answer += option[:1]
                matched = True
                break
        if not matched:
            answer += best_option_by_similarity(part, options, threshold=0.8)
    return "".join(sorted(set(answer)))


class TestNormalizeHelpers(unittest.TestCase):
    def test_get_option_text_strips_prefix(self):
        self.assertEqual(get_option_text("A. 网络层"), "网络层")
        self.assertEqual(get_option_text("B、数据链路层"), "数据链路层")
        self.assertEqual(get_option_text("C) 物理层"), "物理层")

    def test_normalize_text_unifies(self):
        self.assertEqual(normalize_text("A. 系统 软件"), "系统软件")
        self.assertEqual(normalize_text("⻛险管理"), normalize_text("风险管理"))


class TestExactOptionLetter(unittest.TestCase):
    def test_exact_match_wins_over_nested_subsequence(self):
        # 这就是独立验证报出的错答案场景: 精确匹配必须选中 C 而不是 A
        self.assertEqual(exact_option_letter("系统软件", NESTED_OPTIONS), "C")
        self.assertEqual(simulate_single_branch("系统软件", NESTED_OPTIONS), "C")

    def test_letter_answer_still_works(self):
        # 字母答案由子序列兜底处理(选项行自带字母前缀)
        self.assertEqual(exact_option_letter("C", NESTED_OPTIONS), "")
        self.assertEqual(simulate_single_branch("C", NESTED_OPTIONS), "C")
        self.assertEqual(simulate_single_branch("B", ENGLISH_OPTIONS), "B")
        self.assertEqual(simulate_single_branch("CPU 是中央处理器", ENGLISH_OPTIONS), "A")

    def test_multiple_choice_text_parts(self):
        parts = ["系统软件", "图形界面"]
        self.assertEqual(simulate_multiple_branch(parts, NESTED_OPTIONS), "BC")

    def test_multiple_choice_glued_letters_via_expansion(self):
        # TikuLocal 会把 "AC" 展开成选项原文, 这里验证展开后的结果能从文本正确落到字母
        parts = ["操作系统是一种系统软件", "系统软件"]
        self.assertEqual(simulate_multiple_branch(parts, NESTED_OPTIONS), "AC")

    def test_no_match(self):
        self.assertEqual(exact_option_letter("完全不相关的内容", NESTED_OPTIONS), "")
        self.assertEqual(exact_option_letter("", NESTED_OPTIONS), "")
        self.assertEqual(exact_option_letter("系统软件", []), "")

    def test_similarity_fallback_threshold(self):
        # "系统软体" vs "系统软件": ratio 0.75, 阈值 0.8 时不采纳(交给随机), 0.7 时采纳 C
        self.assertEqual(best_option_by_similarity("系统软体", NESTED_OPTIONS, threshold=0.8), "")
        self.assertEqual(best_option_by_similarity("系统软体", NESTED_OPTIONS, threshold=0.7), "C")
        self.assertEqual(best_option_by_similarity("完全无关的内容", NESTED_OPTIONS, threshold=0.8), "")


if __name__ == "__main__":
    unittest.main()

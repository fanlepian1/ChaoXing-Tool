# -*- coding: utf-8 -*-
"""本地题库引擎。

职责:
    1. 加载/保存本地题库文件(默认 ``tiku/bank.json``), 支持一个或多个题库文件;
    2. 题目文本归一化 + 精确匹配 + 相似度模糊匹配, 用于命中题库里题面略有差异的题目;
    3. 与 ``cache.json``(运行时自动积累的答案缓存)互转, 让题库可以持续长大。

题库文件支持的几种写法(可以混用, 也可以是这些结构的列表)::

    {                                    # 1. 最简: 题目 -> 答案
        "题干内容": "A"
    }

    {                                    # 2. 带元信息
        "题干内容": {"answer": "A", "type": "single", "source": "xxx"}
    }

    [                                    # 3. TikuAdapter / 常见导出结构
        {"question": "题干", "answer": ["A", "B"]},
        {"title": "题干", "answer": "对"}
    ]

本模块只依赖标准库, 方便单测与独立脚本(scripts/tiku_tool.py)复用。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

__all__ = [
    "DEFAULT_BANK_PATH",
    "DEFAULT_CACHE_PATH",
    "DEFAULT_TIKU_CONFIG",
    "TIKU_ENV_PREFIX",
    "TIKU_ENV_ALIASES",
    "LocalBank",
    "clean_title",
    "compact_text",
    "answer_to_text",
    "collect_entries",
    "collect_tiku_config_from_env",
    "load_bank_file",
    "merge_bank_files",
    "parse_text_entries",
    "split_paths",
]

DEFAULT_BANK_PATH = "tiku/bank.json"
DEFAULT_CACHE_PATH = "tiku/cache.json"

# 题库配置的环境变量约定: CHAOXING_TIKU_XXX -> [tiku] 节里的 xxx
# GitHub Actions 只需要把 secrets 映射成这些环境变量, 密码/token 就不会进仓库
TIKU_ENV_PREFIX = "CHAOXING_TIKU_"
TIKU_ENV_ALIASES = {
    "bank": "local_bank",
    "bank_path": "local_bank",
    "local_bank_path": "local_bank",
    "cache": "cache_file",
    "cache_path": "cache_file",
    "threshold": "local_threshold",
    "fuzzy": "local_fuzzy",
    "go_auth": "go_authorization",
    "sf_key": "siliconflow_key",
    "export": "export_bank",
    "no_submit": "submit",
}

# [tiku] 配置的兜底默认值(优先级: 默认值 < config.ini < 环境变量 < 命令行)
DEFAULT_TIKU_CONFIG: dict = {
    "provider": "",               # 留空=不启用题库; auto=按已配置的凭据自动组回退链
    "submit": "false",
    "cover_rate": "0.9",
    "delay": "1.0",
    "true_list": "正确,对,√,是,true,t,yes,y,1",
    "false_list": "错误,错,×,否,不对,不正确,false,f,no,n,0",
    "local_bank": DEFAULT_BANK_PATH,
    "local_fuzzy": "true",
    "local_threshold": "0.86",
    "cache_file": DEFAULT_CACHE_PATH,
    "export_bank": "false",
}


def collect_tiku_config_from_env(env: dict = None) -> dict:
    """收集 CHAOXING_TIKU_XXX 环境变量, 转成 [tiku] 配置键。"""
    env = os.environ if env is None else env
    conf: dict = {}
    for key, value in env.items():
        if not str(key).upper().startswith(TIKU_ENV_PREFIX):
            continue
        raw = "" if value is None else str(value).strip()
        if not raw:
            continue
        name = str(key)[len(TIKU_ENV_PREFIX):].strip().lower()
        conf[TIKU_ENV_ALIASES.get(name, name)] = raw
    return conf

# 常见题库导出/接口里答案字段的别名
_ANSWER_KEYS = ("answer", "答案", "bestAnswer", "answerText", "correct", "correctAnswer", "right",
                "value", "content", "selectedOptions", "blanks", "isCorrect")
_QUESTION_KEYS = ("question", "title", "题干", "题目", "questionTitle", "stem", "queston")
_TYPE_KEYS = ("type", "questionType", "题型", "qtype")
# 这些键自身是元信息, 不能当成"题目文本"来收录
_META_KEYS = set(_ANSWER_KEYS) | set(_TYPE_KEYS) | {"plat", "code", "msg", "message", "times", "source"}

# 题干首部的题号 / 题型标签 / 分值后缀, 这些与题目本身无关, 归一化时去掉。
#
# 注意: 题号**必须带分隔符或括号**才算题号。否则 "3D打印技术" 会被削成 "d打印技术",
# "1921年中国共产党成立" 会变成 "年中国共产党成立" —— 这样不同题目会归一化成同一个 key,
# 导致精确匹配假命中(静默给出错误答案), 而且导入/保存时还会把两道题合并成一条。
_RE_LEADING_NUMBER = re.compile(
    r"^\s*(?:"
    r"[（(\[]\s*\d{1,4}\s*[)）\]]"      # (1) / （12） / [3]
    r"|\d{1,4}\s*[、.．,，:：]"          # 1、 / 12. / 3：
    r"|\d{1,4}\s*[)）\]]"                # 1) / 2） / 3]
    r"|第\s*\d{1,4}\s*题"                # 第3题
    r")\s*(?![0-9])"                     # "3.5GHz" 这种小数不算题号
    # 注意: 刻意**不**处理 "3.5 题干" 这种小节编号 —— 它和 "3.5 英寸是几厘米"
    # 这类"内容是小数"的题干无法区分, 处理了就必然错答, 宁可漏配(交给包含式模糊兜底)。
)
# 题型标签: 只有括号里是**题型词**或**短编号**时才当标签剥掉。
# 否则 "[0,1]区间上的函数" / "【0,1】区间上的函数有界" / "【2024】大事记" 这种题面会被削成同一个 key,
# 造成精确匹配假命中(静默错答案)与导入时丢题 —— 这与题号过度删减是同一类事故。
# 说明: 括号里的纯数字只认 1~3 位(题号/序号), 4 位很可能是年份(内容)所以保留。
_LABEL_WORDS = (r"(?:单选题|多选题|判断题|填空题|简答题|选择题|多项选择题|单项选择|多项选择|不定项选择题|"
                r"名词解释|论述题|计算题|分析题|单选|多选|判断|填空|简答|选择|题目|题干)")
_RE_TAG = re.compile(
    r"^\s*(?:"
    r"【\s*(?:\d{1,3}|" + _LABEL_WORDS + r")\s*】"
    r"|\[\s*(?:\d{1,3}|" + _LABEL_WORDS + r")\s*\]"
    r"|[（(]\s*(?:\d{1,3}|" + _LABEL_WORDS + r")\s*[)）]"
    r")\s*"
)
_RE_SCORE_SUFFIX = re.compile(r"\s*[（(]\s*\d+(?:\.\d+)?\s*分\s*[)）]\s*$")
# 分值也可能出现在题面**开头**(题库导出常见: "（2.0分）下列说法正确的是")
_RE_SCORE_PREFIX = re.compile(r"^\s*[（(]\s*\d+(?:\.\d+)?\s*分\s*[)）]\s*")
_RE_VARIANT = str.maketrans({"⻛": "风", "⻔": "门", "⻋": "车", "⻢": "马", "⺠": "民", "⻓": "长"})
# 全角 ASCII -> 半角
_FULLWIDTH = str.maketrans({chr(0xFF01 + i): chr(0x21 + i) for i in range(0x5E)})
_WHITESPACE_RE = re.compile(r"\s+")

# 中文/全角标点先规范化成 ASCII 对应物, 再分类:
#   6—4 与 6-4 视为同一题、3。14 与 3.14 视为同一题、1、2 与 1,2 视为同一题;
#   (0,1) 与 [0,1] 的差异保留(【】是 [] 的中文写法, 视为同一种括号)
_CANONICALIZE = {
    "\u3002": ".", "\u3001": ",", "\u00b7": ".", "\uff0e": ".",
    "\u2014": "-", "\u2013": "-", "\u2212": "-", "\u2010": "-", "\u2011": "-",
    "\u3010": "[", "\u3011": "]", "\u3014": "[", "\u3015": "]",
}
# 纯格式噪音: 精确匹配时直接丢弃
_DROP_CHARS = frozenset(
    " \t\r\n\u3000"
    "\u2026\uff5e\u2027"
    "「」『』《》"
)
# 引号类: 平时是格式噪音(「“一带一路”」), 但夹在数字中间时算内容(3“5 不能等于 35)
_QUOTE_CHARS = frozenset("\"'“”‘’`")
# 括号类: 精确匹配时保留(区分 (0,1) 与 [0,1]), 模糊匹配时允许两边"只有括号不同"
_NOISE_CHARS = frozenset("()[]{}（）〈〉")
# 承载语义的 ASCII 运算符
_SIGNIFICANT_CHARS = frozenset("+-*/=%&|^_~@#$<>")
# 句读/数值共用的一批标点: ?/! 永远算句读; . , : ; 只在"两侧都是数字"或
# "两侧都是 ASCII 字母数字"时算内容(3.5 / 1,2 / 点(x,y)), 否则算句读丢弃
_SENTENCE_PUNCT = frozenset("?!")
_AMBIGUOUS_PUNCT = frozenset(".,:;")

# 纯文本题库的行分隔符(按优先级)与题型别名
TEXT_SEPARATORS = ("----", "$$", "\t", "==>", "==", "|", " - ", "：")
TEXT_TYPE_ALIASES = {
    "single": "single", "单选": "single", "单选题": "single",
    "multiple": "multiple", "多选": "multiple", "多选题": "multiple",
    "judgement": "judgement", "判断": "judgement", "判断题": "judgement",
    "completion": "completion", "填空": "completion", "填空题": "completion",
}


def _is_word_char(ch: str) -> bool:
    """词字符: 字母/数字/汉字等, 归一化后原样保留。"""
    return ch.isalnum()


def compact_text(text: Any) -> str:
    """把题面归一化成**无损**的精确匹配键。

    步骤: 去题号/题型标签/分值 -> 统一异体字与全半角 -> 中文标点规范化 -> 去空白;
          词字符(字母/数字/汉字)原样保留; 句读标点丢弃; 括号/运算符等编码成 `~<十六进制>,`。

    为什么 token 要带分隔符: 键里数字与 a-f 是**原样保留**的, 若 token 不以分隔符结束,
    正则会把后面的内容字符一起吞掉(`~283~2e5~29` 会被读成 `~283`), 导致签名与宽松形式被破坏。
    """
    if text is None:
        return ""
    s = str(text).translate(_FULLWIDTH).strip()
    for _ in range(3):
        new = _RE_TAG.sub("", s)
        new = _RE_LEADING_NUMBER.sub("", new)
        new = _RE_SCORE_PREFIX.sub("", new)
        new = _RE_SCORE_SUFFIX.sub("", new)
        if new == s:
            break
        s = new
    s = s.translate(_RE_VARIANT)
    s = _WHITESPACE_RE.sub("", s)
    chars = [_CANONICALIZE.get(ch, ch) for ch in s.lower()]
    parts = []
    length = len(chars)
    for index, ch in enumerate(chars):
        if _is_word_char(ch):
            parts.append(ch)
            continue
        if ch in _DROP_CHARS or ch in _SENTENCE_PUNCT:
            continue
        if ch in _QUOTE_CHARS:
            prev_ch = chars[index - 1] if index > 0 else ""
            next_ch = chars[index + 1] if index + 1 < length else ""
            if prev_ch.isdigit() and next_ch.isdigit():
                parts.append(f"~{ord(ch):x},")
            continue
        if ch in _AMBIGUOUS_PUNCT:
            prev_ch = chars[index - 1] if index > 0 else ""
            next_ch = chars[index + 1] if index + 1 < length else ""
            both_digits = prev_ch.isdigit() and next_ch.isdigit()
            both_ascii_word = (prev_ch.isascii() and prev_ch.isalnum()
                               and next_ch.isascii() and next_ch.isalnum())
            if both_digits or both_ascii_word:
                parts.append(f"~{ord(ch):x},")
            continue
        parts.append(f"~{ord(ch):x},")
    return "".join(parts)


def _tokens(key: str) -> list[str]:
    """取回精确键里的编码 token(带分隔符, 不会吞掉后面的内容字符)。"""
    return re.findall(r"~([0-9a-f]{1,6}),", key)


def _token_char(token: str) -> Optional[str]:
    try:
        code = int(token, 16)
    except (TypeError, ValueError):
        return None
    if code < 0 or code > 0x10FFFF:
        return None
    return chr(code)


def loose_key(key: str) -> str:
    """精确键 -> 只保留词字符的宽松形式(用于模糊匹配)。"""
    return re.sub(r"~[0-9a-f]{1,6},", "", key)


def noise_signature(key: str) -> str:
    """格式噪音(括号类)签名: 用于区分"只有括号种类不同"的不同题目。"""
    return "".join(f"~{token}," for token in _tokens(key) if _token_char(token) in _NOISE_CHARS)


def significant_signature(key: str) -> str:
    """承载语义的标点签名(运算符 + 紧邻数字的数值标点): 只要不同就判为不同题目。"""
    return "".join(f"~{token}," for token in _tokens(key) if _token_char(token) not in _NOISE_CHARS)


def _is_ascii_word(ch: str) -> bool:
    return bool(ch) and ch.isascii() and ch.isalnum()


def _containment_cuts_word(shorter: str, longer: str) -> bool:
    """包含匹配是否切在 ASCII 字母数字串中间(那种匹配不可信:
    'm 是什么单位' 是 'cm 是什么单位' 的子串, 但两道题答案不同)。"""
    start = longer.find(shorter)
    if start < 0:
        return False
    end = start + len(shorter)
    cut_left = start > 0 and _is_ascii_word(longer[start - 1]) and _is_ascii_word(longer[start])
    cut_right = end < len(longer) and _is_ascii_word(longer[end - 1]) and _is_ascii_word(longer[end])
    return cut_left or cut_right


def clean_title(text: Any) -> str:
    """清理题面里与题目无关的部分(题号/题型标签/分值), 但保留原文(含标点)。

    用于写入缓存或发给第三方题库前的预处理: 题号与分值经常导致外部题库匹配失败,
    而标点(尤其全角标点)是题库索引的一部分, 这里刻意不做转换。
    """
    if text is None:
        return ""
    s = str(text).strip()
    for _ in range(3):
        new = _RE_TAG.sub("", s)
        new = _RE_LEADING_NUMBER.sub("", new)
        new = _RE_SCORE_PREFIX.sub("", new)
        new = _RE_SCORE_SUFFIX.sub("", new)
        if new == s:
            break
        s = new
    return s.strip()


def answer_to_text(answer: Any) -> Optional[str]:
    """把各种形态的答案统一成字符串(base.py 用换行分隔多答案)。"""
    if answer is None:
        return None
    if isinstance(answer, bool):
        return "正确" if answer else "错误"
    if isinstance(answer, str):
        text = answer.strip()
        return text or None
    if isinstance(answer, (int, float)):
        return str(answer)
    if isinstance(answer, dict):
        for key in ("bestAnswer", "selectedOptions", "blanks"):
            if key in answer:
                got = answer_to_text(answer[key])
                if got:
                    return got
        if "isCorrect" in answer:
            return answer_to_text(bool(answer["isCorrect"]))
        for key in _ANSWER_KEYS:
            if key in answer:
                got = answer_to_text(answer[key])
                if got:
                    return got
        return None
    if isinstance(answer, (list, tuple)):
        parts = [p for p in (answer_to_text(item) for item in answer) if p]
        return "\n".join(parts) if parts else None
    if isinstance(answer, (set, frozenset)):
        parts = sorted(p for p in (answer_to_text(item) for item in answer) if p)
        return "\n".join(parts) if parts else None
    return str(answer).strip() or None


def _pick(mapping: dict, keys: Iterable[str]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _entry_from_question_answer(question: Any, answer: Any, qtype: Any = None, source: Optional[str] = None,
                                own_source: Any = None, own_auto: Any = None) -> Optional[dict]:
    # 注: 纯文本行里带的内部字段(_line)不会进 entry
    text = str(question).strip() if question is not None else ""
    if not text:
        return None
    ans = answer_to_text(answer)
    if not ans:
        return None
    key = compact_text(text)
    if not key:
        return None
    entry = {"question": text, "answer": ans, "key": key}
    if qtype:
        entry["type"] = str(qtype).strip()
    # 条目自带的来源优先于文件名: 它记录的是"这条答案从哪来"(cache:xxx / manual / 某个题库),
    # 被文件名覆盖掉的话, 回写时就没法区分"缓存自动写的"和"人工核对的"了
    source_value = own_source if (isinstance(own_source, str) and own_source.strip()) else source
    if source_value:
        entry["source"] = str(source_value).strip()
    if own_auto is not None and str(own_auto).strip():
        entry["auto_answer"] = str(own_auto).strip()
    return entry


def collect_entries(payload: Any, source: Optional[str] = None, stats: Optional[dict] = None) -> list[dict]:
    """把任意一种题库 JSON 结构摊平成 entry 列表。

    stats 传入一个 dict 时, 会写入 {"skipped_empty": 有题目但没答案的条目数},
    方便调用方提示用户"你的题库里有几条答案为空"而不是静默丢掉。
    """
    entries: list[dict] = []

    def note_skipped() -> None:
        if stats is not None:
            stats["skipped_empty"] = stats.get("skipped_empty", 0) + 1

    def walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, dict):
            # 形如 {"question": ..., "answer": ...} 的单条记录
            q_key = _pick(node, _QUESTION_KEYS)
            a_key = _pick(node, _ANSWER_KEYS)
            if q_key is not None and a_key is not None and not isinstance(q_key, (dict, list)):
                entry = _entry_from_question_answer(q_key, a_key, _pick(node, _TYPE_KEYS), source,
                                                    own_source=node.get("source"),
                                                    own_auto=node.get("auto_answer"))
                if entry:
                    entries.append(entry)
                else:
                    note_skipped()
                return  # 已当作单条记录处理完, 不能再按"题目->答案"映射扫一遍
            # 形如 {"answer": {...}, "plat": 0} 的包装
            if a_key is not None and isinstance(a_key, (dict, list)):
                walk(a_key)
                return
            # 普通映射: 题目 -> 答案
            for key, value in node.items():
                if str(key).strip().lower() in _META_KEYS:
                    continue
                if isinstance(value, (dict, list)):
                    if isinstance(value, dict):
                        inner = _pick(value, _ANSWER_KEYS)
                        if inner is not None:
                            entry = _entry_from_question_answer(key, inner, _pick(value, _TYPE_KEYS), source,
                                                                own_source=value.get("source"),
                                                                own_auto=value.get("auto_answer"))
                            if entry:
                                entries.append(entry)
                                continue
                    walk(value)
                    continue
                entry = _entry_from_question_answer(key, value, None, source)
                if entry:
                    entries.append(entry)
                else:
                    note_skipped()  # 有题目但答案为空, 记下来提示用户而不是静默丢弃
            return
        if isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return entries


def _read_text(path: Any) -> str:
    """读取文本文件, 容忍 UTF-8 BOM(Windows 记事本/Excel 另存的题库都会带 BOM)。"""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError:
        return ""
    if not raw:
        return ""
    return raw.decode("utf-8-sig", errors="ignore")


JSON_SUFFIXES = (".json", ".jsonl")


def looks_like_json(text: str) -> bool:
    """内容是否"像 JSON"。

    只看首个字符是 {/[ 是不够的: 纯文本题库的第一行经常是 "[单选题]题干----A",
    甚至带引号("[判断题]What's this----正确")。这里采用"JSON 优先 + 文本兜底"的策略:
    只要严格解析成功就按 JSON 走, 解析失败时再判断能不能当文本题库读(见 load_bank_file_with_stats),
    并且用 _looks_like_json_fragment 把"JSON 碎片"这种伪题目挡掉。
    """
    if not text:
        return False
    head = text.lstrip()[:1]
    return head in ("{", "[")


def _line_looks_like_json_junk(line: str) -> bool:
    """整行以 {/[ 开头、却又没有任何文本题库分隔符 -> 几乎肯定是坏 JSON 的碎片。"""
    stripped = str(line or "").strip()
    if not stripped or stripped[0] not in "{[":
        return False
    return not any(sep in stripped for sep in TEXT_SEPARATORS)


def looks_like_json_fragment(question: Any, line: Any = None) -> bool:
    """判断一条"题目"其实是不是被截断的 JSON 碎片(那种条目绝不能进题库)。

    只挡真正的碎片, 不误杀合法题面(例如 `[选] 关于 HTTP 的说法：正确的是`、
    `["一带一路"]指的是什么倡议`、`字典 {"a": 1} 在 Python 里叫什么`)。
    """
    text = str(question or "").strip()
    if not text:
        return True
    if line is not None and _line_looks_like_json_junk(str(line)):
        return True
    if text[0] not in "{[":            # 不以 {/[ 开头的一律当正常题目
        return False
    # {/[ 紧跟引号, 且没有闭合的括号 -> 是被截断的 JSON 片段
    if re.match(r'^\s*[\[{]\s*["\']', text):
        if text[0] == "{" and "}" not in text:
            return True
        if text[0] == "[" and "]" not in text:
            return True
        return False
    return False


def load_bank_file_with_stats(path: str | Path) -> tuple[list[dict], dict]:
    """读取题库文件, 同时回报问题(坏 JSON / 答案为空的条目数), 供上层提示用户。

    格式判定:
        * ``.json`` / ``.jsonl`` 后缀 -> 只能是 JSON, 解析失败就跳过并报 bad_json
          (绝不退化成文本解析: 截断的 JSON 会被切成"JSON 片段当题目/答案"的垃圾条目,
           那些垃圾条目还能模糊命中真题);
        * 其他后缀 -> 内容像 JSON 则按 JSON 解析, 否则按纯文本(题目----答案)解析。
    """
    stats: dict = {}
    p = Path(path)
    if not p.is_file():
        return [], stats
    raw = _read_text(p)
    if not raw.strip():
        # 0 字节/纯空白: 很可能是"写到一半被打断"的残留。当成可疑文件, 拒绝覆盖它(见 failed_paths)。
        try:
            if p.stat().st_size == 0:
                stats["empty_file"] = True
        except OSError:
            pass
        return [], stats

    is_json_name = p.suffix.lower() in JSON_SUFFIXES
    if p.suffix.lower() == ".jsonl":
        # 行分隔 JSON: 每行一个对象/数组, 坏行跳过(其余仍然可用)
        entries_jsonl: list[dict] = []
        bad_lines = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            entries_jsonl.extend(collect_entries(item, source=p.name, stats=stats))
        if bad_lines:
            stats["skipped_lines"] = stats.get("skipped_lines", 0) + bad_lines
        if not entries_jsonl and bad_lines:
            stats["bad_json"] = True
        return entries_jsonl, stats
    if is_json_name or looks_like_json(raw):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            if is_json_name:
                stats["bad_json"] = True
                return [], stats
            # 后缀不是 .json 却以 {/[ 开头: 可能是纯文本题库(首行 [单选题]…), 也可能是截断的 JSON。
            # 试着按文本解析, 但必须把"JSON 碎片"这种伪题目挡掉, 否则会拿 JSON 片段当答案。
            text_entries, skipped = parse_bank_text(raw)
            if text_entries:
                if skipped:
                    stats["skipped_lines"] = skipped
                return collect_entries(text_entries, source=p.name, stats=stats), stats
            stats["bad_json"] = True
            return [], stats
        return collect_entries(payload, source=p.name, stats=stats), stats

    entries, skipped = parse_bank_text(raw)
    if skipped:
        stats["skipped_lines"] = skipped
    if not entries:
        return [], stats
    return collect_entries(entries, source=p.name, stats=stats), stats


def load_bank_file(path: str | Path) -> list[dict]:
    """读取单个题库文件(见 load_bank_file_with_stats), 返回条目列表。"""
    entries, _stats = load_bank_file_with_stats(path)
    return entries


def parse_text_entries(text: str) -> tuple[list[dict], int]:
    """解析纯文本题库, 返回 (entries, 跳过的行数)。

    支持的行格式(自动识别分隔符)::

        题目内容----答案
        题目内容$$答案
        题目内容<TAB>答案
        题目内容,答案              (csv: 第一列题目 + 最后一列答案)
        题目内容|答案|单选          (最后一列可选, 表示题型)
    """
    entries: list[dict] = []
    skipped = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        question = ""
        answer = ""
        q_type = None

        for sep in TEXT_SEPARATORS:
            if sep in line:
                head, _, tail = line.partition(sep)
                question, answer = head.strip(), tail.strip()
                # 形如 "答案|单选" 的尾列: 抽出题型
                for inner_sep in ("|", ",", "\t"):
                    if inner_sep in answer:
                        parts = [part.strip() for part in answer.split(inner_sep) if part.strip()]
                        if len(parts) >= 2 and parts[-1].lower() in TEXT_TYPE_ALIASES:
                            q_type = TEXT_TYPE_ALIASES[parts[-1].lower()]
                            answer = inner_sep.join(parts[:-1])
                        break
                break
        if not question:
            parts = [part.strip() for part in line.split(",") if part.strip()]
            if len(parts) >= 2:
                question, answer = parts[0], parts[-1]
                for part in parts[1:-1]:
                    if part.lower() in TEXT_TYPE_ALIASES:
                        q_type = TEXT_TYPE_ALIASES[part.lower()]
                        break
        if question and answer:
            entry = {"question": question, "answer": answer, "_line": line}
            if q_type:
                entry["type"] = q_type
            entries.append(entry)
        else:
            skipped += 1
    return entries, skipped




def _path_key(path: Any) -> str:
    """路径归一化(解析为绝对路径 + 统一大小写)用于比较, 避免换个写法就绕过写保护。"""
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path)
    return os.path.normcase(str(resolved))


def parse_bank_text(raw: str) -> tuple[list[dict], int]:
    """按纯文本题库格式解析, 并过滤掉"其实是被截断的 JSON 碎片"的伪条目。

    返回 (可用条目, 被跳过的行数)。被过滤掉的伪条目也会计入跳过数, 避免"悄悄少了几道题"。
    """
    entries, skipped = parse_text_entries(raw)
    credible = [item for item in entries
                if not looks_like_json_fragment(item.get("question"), item.get("_line"))]
    skipped += len(entries) - len(credible)
    return credible, skipped



def split_paths(value: Any) -> list[str]:
    """把 ``a.json,b.json`` / list / None 统一成路径列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = [str(v) for v in value]
    else:
        items = [part for part in re.split(r"[,;\n]", str(value)) if part.strip()]
    return [it.strip() for it in items if it and it.strip()]


class LocalBank:
    """本地题库: 多文件加载 + 精确/模糊匹配 + 可写回。

    匹配策略(逐级放开, 但**默认不会给出错答案**):
        1. 精确匹配: 归一化后完全一致 —— 覆盖题号/标签/分值/标点/全半角/空白差异;
        2. 包含匹配(fuzzy): 归一化后一方完整包含另一方, 且长度比例 >= contain_rate。
           只允许"多几个字/少几个字"的差异(例如题库题面比卷面多了"下列哪一项"),
           不允许中间替换 —— 因为"世界上最大的洋/洲"这类仅差一字的不同题目
           必须判为不命中, 否则会提交错误答案;
        3. 相似度匹配(similarity, 默认关闭): difflib 比率 >= threshold 且带歧义保护。
           开启后对截断严重的 OCR 题面更友好, 但存在"仅差一两字的不同题目"误配风险,
           需要用户自己权衡。
    """

    def __init__(
        self,
        paths: Any = None,
        fuzzy: bool = True,
        threshold: float = 0.9,
        contain_rate: float = 0.75,
        similarity: bool = False,
        min_length: int = 5,
        reload_interval: float = 5.0,
    ) -> None:
        self.paths = [Path(p) for p in (split_paths(paths) or [DEFAULT_BANK_PATH])]
        self.fuzzy = bool(fuzzy)
        self.similarity = bool(similarity)
        self.min_length = max(1, int(min_length or 1))
        try:
            self.threshold = min(1.0, max(0.5, float(threshold)))
        except (TypeError, ValueError):
            self.threshold = 0.9
        try:
            self.contain_rate = min(1.0, max(0.5, float(contain_rate)))
        except (TypeError, ValueError):
            self.contain_rate = 0.75
        self.reload_interval = max(0.0, float(reload_interval or 0))

        self._lock = threading.RLock()
        self._entries: list[dict] = []
        self._exact: dict[str, dict] = {}
        self._loaded_at = 0.0
        self._mtimes: dict[str, float] = {}
        self.loaded_files: list[str] = []
        self.issues: list[str] = []  # 加载时发现的问题(坏 JSON / 空答案条目), 供上层提示
        self.failed_paths: set[str] = set()  # 解析失败的文件: 绝不能被 save() 覆盖掉
        self.last_refusal: Optional[str] = None  # 最近一次 save() 被安全规则拦住的原因

    # ------------------------------------------------------------------ load
    def load(self, force: bool = True) -> int:
        """加载题库, 返回本次加载后的题库条目数。

        加载过程中发现的问题(坏 JSON / 答案为空的条目)会记录在 self.issues 里,
        由上层打日志提示, 而不是静默忽略。
        """
        issues: list[str] = []
        failed_paths: set[str] = set()
        with self._lock:
            entries: list[dict] = []
            loaded_files: list[str] = []
            for path in self.paths:
                file_entries, stats = load_bank_file_with_stats(path)
                if stats.get("bad_json"):
                    issues.append(f"{path}: 是 JSON 但解析失败(文件可能被截断或损坏), 已跳过该文件")
                    failed_paths.add(_path_key(path))
                if stats.get("empty_file"):
                    issues.append(f"{path}: 是 0 字节空文件(可能上次写入被中断), 已跳过并拒绝覆盖该文件")
                    failed_paths.add(_path_key(path))
                if stats.get("skipped_empty"):
                    issues.append(f"{path}: 有 {stats['skipped_empty']} 条题目答案为空, 已跳过(建议在题库里补齐或删掉)")
                if file_entries:
                    entries.extend(file_entries)
                    loaded_files.append(str(path))
                try:
                    self._mtimes[str(path)] = path.stat().st_mtime
                except OSError:
                    self._mtimes.pop(str(path), None)
            self._entries = entries
            self._exact = {}
            for entry in entries:
                # 同一题面后加载的覆盖先加载的
                self._exact[entry["key"]] = entry
            self._loaded_at = time.time()
            self.loaded_files = loaded_files
            self.issues = issues
            self.failed_paths = failed_paths
            return len(self._exact)

    def _maybe_reload(self) -> None:
        if self.reload_interval <= 0:
            return
        if time.time() - self._loaded_at < self.reload_interval:
            return
        changed = False
        for path in self.paths:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = None
            if self._mtimes.get(str(path)) != mtime:
                changed = True
                break
        if changed:
            self.load(force=True)
        else:
            self._loaded_at = time.time()

    # --------------------------------------------------------------- lookup
    def lookup(self, question: Any, qtype: Optional[str] = None) -> Optional[str]:
        """查询答案: 精确 -> 包含式模糊 -> (可选)相似度兜底。"""
        key = compact_text(question)
        if not key:
            return None
        with self._lock:
            self._maybe_reload()
            entry = self._exact.get(key)
            if entry:
                return entry["answer"]
            if not self.fuzzy or len(key) < self.min_length:
                # 过短的题目只允许精确命中: 一字之差就可能是完全不同的题
                return None

            query_loose = loose_key(key)
            query_noise = noise_signature(key)
            query_significant = significant_signature(key)
            if len(query_loose) < self.min_length:
                return None
            candidates: list[tuple[float, dict]] = []
            for entry in self._entries:
                other = entry["key"]
                if not other:
                    continue
                other_loose = loose_key(other)
                if len(other_loose) < self.min_length:
                    continue
                # 护栏 1: 两边都带格式噪音(括号/引号)但种类不同 -> 是"只有括号不同"的不同题目
                other_noise = noise_signature(other)
                if query_noise and other_noise and query_noise != other_noise:
                    continue
                # 护栏 2: 承载语义的标点(. , + - 等)只要不同就是不同题目(3.5 vs 35、1+1 vs 11)
                if query_significant != significant_signature(other):
                    continue
                score = self._contain_score(query_loose, other_loose)
                if score is None and self.similarity:
                    score = self._similarity_score(query_loose, other_loose, qtype)
                if score is not None:
                    candidates.append((score, entry))
            return self._pick_best(candidates)

    def _contain_score(self, key: str, other: str) -> Optional[float]:
        """一方完整包含另一方时给分(只容忍多字/少字, 不容忍中间替换)。

        额外规则: 不允许切在 ASCII 字母数字串中间, 否则 'm 是什么单位' 会命中 'cm 是什么单位'。
        """
        if key == other:
            return 1.0
        if key in other:
            shorter, longer = key, other
        elif other in key:
            shorter, longer = other, key
        else:
            return None
        if _containment_cuts_word(shorter, longer):
            return None
        rate = len(shorter) / len(longer)
        if rate < self.contain_rate:
            return None
        return 0.9 + 0.1 * rate

    def _similarity_score(self, key: str, other: str, qtype: Optional[str]) -> Optional[float]:
        """difflib 相似度(默认关闭): 需要用户显式接受"仅差几个字"的误配风险。"""
        max_len_gap = max(6, int(len(key) * 0.3))
        if abs(len(other) - len(key)) > max_len_gap:
            return None
        matcher = SequenceMatcher(None, key, other)
        if matcher.quick_ratio() < self.threshold:
            return None
        score = matcher.ratio()
        if score < self.threshold:
            return None
        # 安全规则: 只差一两个字的题目, 与"另一道题(正确/错误、属于/不属于 这类否定变体)"无法区分,
        # 一律判为不命中 —— 提交错误答案比不命中更糟。
        edits = sum(max(i2 - i1, j2 - j1)
                    for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal")
        if edits <= 2:
            return None
        if qtype and self._type_of(other) == str(qtype).strip():
            score += 0.02  # 题型一致的轻微加权
        return score

    def _type_of(self, key: str) -> Optional[str]:
        entry = self._exact.get(key)
        return entry.get("type") if entry else None

    @staticmethod
    def _pick_best(candidates: list[tuple[float, dict]]) -> Optional[str]:
        """挑最优候选; 若两个不同答案的候选分数接近, 说明无法确定, 宁可放弃。"""
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        best_score, best_entry = candidates[0]
        for score, entry in candidates[1:]:
            if entry["answer"] != best_entry["answer"] and score >= best_score - 0.03:
                return None
        return best_entry["answer"]

    # ----------------------------------------------------------------- write
    def add(self, question: Any, answer: Any, qtype: Any = None, source: Optional[str] = None,
            auto: bool = False) -> bool:
        """加入/更新一条题目(内存中), 返回是否发生变化。

        auto=True 表示这次写入来自自动回写(缓存/外部题库), 会记录 auto_answer,
        这样下一轮能够区分"没人动过的自动答案"(可继续更新) 与"人工改过的答案"(必须保留)。
        """
        entry = _entry_from_question_answer(question, answer, qtype, source)
        if not entry:
            return False
        if auto:
            entry["auto_answer"] = entry["answer"]
        else:
            entry.pop("auto_answer", None)
        with self._lock:
            old = self._exact.get(entry["key"])
            self._exact[entry["key"]] = entry
            if old is None:
                self._entries.append(entry)
                return True
            for idx, item in enumerate(self._entries):
                if item["key"] == entry["key"]:
                    self._entries[idx] = entry
                    break
            return old["answer"] != entry["answer"]

    def merge(self, payload: Any, source: Optional[str] = None, overwrite: Any = True) -> dict:
        """合并任意结构的一次性题库数据。

        overwrite 三种取值:
            * True  —— 一律覆盖(用户显式 import 时的行为);
            * False —— 只补新题, 已有的一律不动;
            * "auto"(默认给 import_cache 用) —— 只覆盖**来源是缓存**的条目(它的 source 以 cache: 开头)。
              这样既不会覆盖人工核对过/导入的答案, 又不会把之前自动写错、后来搜对了的答案永久锁死。
        返回 {"added", "overwritten", "skipped", "entries"}。
        """
        added = overwritten = skipped = 0
        for entry in collect_entries(payload, source=source):
            key = entry["key"]
            with self._lock:
                old = self._exact.get(key)
            if old is not None and not self._can_overwrite(old, overwrite):
                if old["answer"] != entry["answer"]:
                    skipped += 1
                continue
            changed = self.add(entry["question"], entry["answer"], entry.get("type"),
                               entry.get("source") or source, auto=(overwrite is not True))
            if old is None:
                added += 1
            elif changed:
                overwritten += 1
        return {"added": added, "overwritten": overwritten, "skipped": skipped, "entries": len(self)}

    @staticmethod
    def _can_overwrite(old_entry: dict, overwrite: Any) -> bool:
        if overwrite is True:
            return True
        if overwrite is False:
            return False
        # "auto": 只覆盖"上一次由系统自动写入、并且之后没人改过"的条目。
        # 判据 1: auto_answer 记录的就是上次自动写的答案, 当前答案与它不同 => 人工改过 => 不能覆盖。
        auto_answer = old_entry.get("auto_answer")
        if auto_answer is not None and str(auto_answer).strip():
            return str(old_entry.get("answer", "")).strip() == str(auto_answer).strip()
        # 判据 2: 没有 auto_answer(旧版本写出的文件, 或人工维护的条目)时**无法证明没人动过**,
        # 于是选择保护数据: 不自动覆盖。需要覆盖时用 --overwrite / import / --sync-cache 显式操作。
        return False

    def import_cache(self, cache_path: Any = DEFAULT_CACHE_PATH, source: Optional[str] = None,
                     overwrite: Any = "auto") -> dict:
        """把答案缓存(cache.json: 题目 -> 答案)合并进题库。

        默认 overwrite="auto": 只补新题 + 更新之前由缓存写进去的条目,
        保护你在题库里人工核对过的答案, 同时避免"上次写错了就永远改不回来"。
        """
        empty = {"added": 0, "overwritten": 0, "skipped": 0, "entries": len(self)}
        path = Path(cache_path)
        if not path.is_file():
            return empty
        raw = _read_text(path)
        if not looks_like_json(raw):
            # 缓存文件只可能是 JSON, 内容不像 JSON 说明文件坏了, 不要退化成文本解析
            return empty
        try:
            payload = json.loads(raw or "{}")
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return empty
        if not isinstance(payload, dict):
            return empty
        return self.merge(payload, source=source or f"cache:{path.name}", overwrite=overwrite)

    def export_to_cache(self, cache_path: Any = DEFAULT_CACHE_PATH, overwrite: bool = True) -> int:
        """把题库里的答案写进答案缓存(题库优先)。

        为什么需要: Tiku.query 是先查缓存的, 所以你手动改了题库里的答案后, 旧缓存还会被优先使用。
        改完题库跑一次 `tiku_tool import --sync-cache` 就能让新答案立刻生效。
        返回写入(或更新)的条数。
        """
        path = Path(cache_path)
        self.last_refusal = None
        payload: dict = {}
        if path.exists() and path.suffix.lower() not in JSON_SUFFIXES:
            self.last_refusal = f"{path}: 缓存文件不是 JSON, 为避免改变文件格式已跳过写入"
            return 0
        if path.is_file():
            raw = _read_text(path)
            try:
                loaded = json.loads(raw or "{}")
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                self.last_refusal = f"{path}: 缓存文件解析失败, 为避免覆盖已跳过写入"
                return 0
            if not isinstance(loaded, dict):
                self.last_refusal = f"{path}: 缓存文件不是 JSON 对象, 为避免覆盖已跳过写入"
                return 0
            payload = loaded
        written = 0
        with self._lock:
            for entry in self._entries:
                # 键必须与 Tiku.query 读缓存用的键一致(它会先做 clean_title 预处理),
                # 否则带题号/分值/题型标签的题库同步后依然命中不了旧条目。
                title = clean_title(entry["question"]) or entry["question"]
                if not overwrite and title in payload:
                    continue
                if payload.get(title) != entry["answer"]:
                    written += 1
                payload[title] = entry["answer"]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fp:
                    fp.write(json.dumps(payload, ensure_ascii=False, indent=4))
                    fp.flush()
                    os.fsync(fp.fileno())
                os.replace(tmp_path, str(path))
            except OSError as e:
                self.last_refusal = f"{path}: 写入失败({e})"
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass
                return 0
        except OSError as e:
            self.last_refusal = f"{path}: 写入失败({e})"
            return 0
        return written

    def stamp_auto_answers(self, source_prefix: str = "cache:") -> int:
        """给"来源是缓存但没记 auto_answer"的旧格式条目补上 auto_answer。

        这批条目是旧版本自动回写进来的, 补上之后它们就能重新被自动纠正(否则永远按人工条目保护)。
        返回补了多少条。
        """
        stamped = 0
        prefix = str(source_prefix or "").strip().lower()
        with self._lock:
            for entry in self._entries:
                if entry.get("auto_answer"):
                    continue
                source = str(entry.get("source") or "").strip().lower()
                if prefix and source.startswith(prefix):
                    entry["auto_answer"] = entry["answer"]
                    stamped += 1
        return stamped

    def save(self, path: Any = None, indent: int = 2, allow_failed_overwrite: bool = False) -> Optional[str]:
        """保存题库到文件(默认第一个路径), 返回写入的文件路径; 因保护而拒绝写入时返回 None。

        三条安全规则:
            1. 按归一化后的题面去重(后写入的答案生效), 文件不会越存越乱;
            2. 加载时解析失败的文件**拒绝写入** —— 否则回写会把用户原始题库(可能只是格式读不对)
               整个覆盖掉;
            3. 已有的非 JSON 题库文件(例如用户自己维护的 `题目----答案` 文本)**拒绝写入** ——
               题库写出永远是 JSON, 静默改写会破坏用户的文件格式。
        确认要覆盖/转换时显式传 allow_failed_overwrite=True。
        """
        target = Path(path) if path else (self.paths[0] if self.paths else Path(DEFAULT_BANK_PATH))
        with self._lock:
            self.last_refusal = None
            if _path_key(target) in self.failed_paths and not allow_failed_overwrite:
                self.last_refusal = f"{target}: 该文件加载时解析失败, 为避免覆盖已跳过写回"
                return None
            if target.is_file() and target.stat().st_size == 0 and not allow_failed_overwrite:
                self.last_refusal = f"{target}: 目标文件是 0 字节(可能是上次写入被中断), 已跳过写回"
                return None
            if target.suffix.lower() not in JSON_SUFFIXES and not allow_failed_overwrite:
                self.last_refusal = (f"{target}: 非 JSON 题库文件(题库写出格式固定为 JSON), "
                                     f"为避免改变你的文件格式已跳过写回; "
                                     f"可先用 tiku_tool.py import 转成 .json 再作为 local_bank")
                return None
            # 按归一化题面去重: 后出现的条目覆盖同 key 的旧条目(dict 保留首次出现的位置)
            deduped: dict[str, dict] = {}
            for entry in self._entries:
                deduped[entry["key"]] = entry

            data: dict[str, Any] = {}
            for entry in deduped.values():
                if entry.get("type") or entry.get("source") or entry.get("auto_answer"):
                    item: dict[str, Any] = {"answer": entry["answer"]}
                    if entry.get("type"):
                        item["type"] = entry["type"]
                    if entry.get("source"):
                        item["source"] = entry["source"]
                    if entry.get("auto_answer"):
                        item["auto_answer"] = entry["auto_answer"]
                    data[entry["question"]] = item
                else:
                    data[entry["question"]] = entry["answer"]
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                # 原子写(临时文件 + os.replace): 避免并发读者看到"写了一半/0 字节"的题库
                fd, tmp_path = tempfile.mkstemp(prefix=target.name + ".", dir=str(target.parent))
                try:
                    if target.suffix.lower() == ".jsonl":
                        body = "".join(
                            json.dumps({key: value}, ensure_ascii=False) + "\n"
                            for key, value in data.items()
                        )
                    else:
                        body = json.dumps(data, ensure_ascii=False, indent=indent) + "\n"
                    with os.fdopen(fd, "w", encoding="utf-8") as fp:
                        fp.write(body)
                        fp.flush()
                        os.fsync(fp.fileno())
                    os.replace(tmp_path, str(target))
                except OSError as e:
                    self.last_refusal = f"{target}: 写入失败({e})"
                    try:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                    except OSError:
                        pass
                    return None
            except OSError as e:
                self.last_refusal = f"{target}: 写入失败({e})"
                return None
        return str(target)

    # ------------------------------------------------------------------ misc
    def __len__(self) -> int:
        with self._lock:
            return len(self._exact)

    def __iter__(self) -> Iterator[dict]:
        return iter(list(self._entries))

    def stats(self) -> dict:
        with self._lock:
            return {
                "entries": len(self._exact),
                "files": list(self.loaded_files),
                "paths": [str(p) for p in self.paths],
            }


def merge_bank_files(inputs: Iterable[str | Path], output: str | Path, threshold: float = 0.9) -> dict:
    """把多个题库文件合并进一个题库文件(后加载的覆盖先加载的)。

    输出文件如果已经存在, 会先把它本身当作一个输入读进来 —— 否则"合并"会静默销毁里面原有的题目。
    """
    input_list = [str(p) for p in inputs]
    output_key = _path_key(output)
    if output_key not in {_path_key(item) for item in input_list} and Path(output).is_file():
        input_list.insert(0, str(output))
    bank = LocalBank(paths=input_list, fuzzy=False, threshold=threshold, reload_interval=0)
    bank.load()
    failed = any(item in bank.failed_paths for item in [_path_key(i) for i in input_list])
    written = bank.save(output)
    return {"entries": len(bank), "output": written, "refusal": bank.last_refusal,
            "issues": list(bank.issues), "failed": failed}

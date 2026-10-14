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

# 题干首部的题号 / 题型标签 / 分值后缀, 这些与题目本身无关, 归一化时去掉
_RE_LEADING_NUMBER = re.compile(r"^\s*(?:[（(\[]\s*)?\d{1,4}\s*(?:[)）\]]\s*)?[、.．,，:：]?\s*")
_RE_TAG = re.compile(r"^\s*(?:【[^】]{0,16}】|\[[^\]]{0,16}\]|（\s*[^）]{0,16}(?:题|选择|判[断定]|填空|简答)\s*）)\s*")
_RE_SCORE_SUFFIX = re.compile(r"\s*[（(]\s*\d+(?:\.\d+)?\s*分\s*[)）]\s*$")
_RE_VARIANT = str.maketrans({"⻛": "风", "⻔": "门", "⻋": "车", "⻢": "马", "⺠": "民", "⻓": "长"})
_RE_DROP = re.compile(r"[\s，。！？；：、,.!?;:'\"“”‘’`~!@#$%^&*\-_+=|\\/<>《》…·（）()\[\]【】{}]")
# 全角 ASCII -> 半角
_FULLWIDTH = str.maketrans({chr(0xFF01 + i): chr(0x21 + i) for i in range(0x5E)})

# 纯文本题库的行分隔符(按优先级)与题型别名
TEXT_SEPARATORS = ("----", "$$", "\t", "==>", "==", "|", " - ", "：")
TEXT_TYPE_ALIASES = {
    "single": "single", "单选": "single", "单选题": "single",
    "multiple": "multiple", "多选": "multiple", "多选题": "multiple",
    "judgement": "judgement", "判断": "judgement", "判断题": "judgement",
    "completion": "completion", "填空": "completion", "填空题": "completion",
}


def compact_text(text: Any) -> str:
    """把题面归一化成用于匹配的紧凑串(去题号/标签/分值/标点/空白, 统一大小写与全半角)。"""
    if text is None:
        return ""
    s = str(text).translate(_FULLWIDTH).strip()
    for _ in range(3):
        new = _RE_TAG.sub("", s)
        new = _RE_LEADING_NUMBER.sub("", new)
        new = _RE_SCORE_SUFFIX.sub("", new)
        if new == s:
            break
        s = new
    s = s.translate(_RE_VARIANT)
    s = _RE_DROP.sub("", s)
    return s.lower()


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


def _entry_from_question_answer(question: Any, answer: Any, qtype: Any = None, source: Optional[str] = None) -> Optional[dict]:
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
    if source:
        entry["source"] = str(source)
    return entry


def collect_entries(payload: Any, source: Optional[str] = None) -> list[dict]:
    """把任意一种题库 JSON 结构摊平成 entry 列表。"""
    entries: list[dict] = []

    def walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, dict):
            # 形如 {"question": ..., "answer": ...} 的单条记录
            q_key = _pick(node, _QUESTION_KEYS)
            a_key = _pick(node, _ANSWER_KEYS)
            if q_key is not None and a_key is not None and not isinstance(q_key, (dict, list)):
                entry = _entry_from_question_answer(q_key, a_key, _pick(node, _TYPE_KEYS), source)
                if entry:
                    entries.append(entry)
                    return
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
                            entry = _entry_from_question_answer(key, inner, _pick(value, _TYPE_KEYS), source)
                            if entry:
                                entries.append(entry)
                                continue
                    walk(value)
                    continue
                entry = _entry_from_question_answer(key, value, None, source)
                if entry:
                    entries.append(entry)
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


def load_bank_file(path: str | Path) -> list[dict]:
    """读取单个题库文件, 支持 JSON 与纯文本两种格式。

    文件不存在或格式不对时返回空列表(不抛异常), 避免一个坏题库文件拖垮整轮刷课。
    """
    p = Path(path)
    if not p.is_file():
        return []
    raw = _read_text(p)
    if not raw.strip():
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        entries, _skipped = parse_text_entries(raw)
        return collect_entries(entries, source=p.name) if entries else []
    return collect_entries(payload, source=p.name)


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
            entry = {"question": question, "answer": answer}
            if q_type:
                entry["type"] = q_type
            entries.append(entry)
        else:
            skipped += 1
    return entries, skipped


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
    """本地题库: 多文件加载 + 精确/模糊匹配 + 可写回。"""

    def __init__(
        self,
        paths: Any = None,
        fuzzy: bool = True,
        threshold: float = 0.86,
        reload_interval: float = 5.0,
    ) -> None:
        self.paths = [Path(p) for p in (split_paths(paths) or [DEFAULT_BANK_PATH])]
        self.fuzzy = bool(fuzzy)
        try:
            self.threshold = min(1.0, max(0.5, float(threshold)))
        except (TypeError, ValueError):
            self.threshold = 0.86
        self.reload_interval = max(0.0, float(reload_interval or 0))

        self._lock = threading.RLock()
        self._entries: list[dict] = []
        self._exact: dict[str, dict] = {}
        self._loaded_at = 0.0
        self._mtimes: dict[str, float] = {}
        self.loaded_files: list[str] = []

    # ------------------------------------------------------------------ load
    def load(self, force: bool = True) -> int:
        """加载题库, 返回本次加载后的题库条目数。"""
        with self._lock:
            entries: list[dict] = []
            loaded_files: list[str] = []
            for path in self.paths:
                file_entries = load_bank_file(path)
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
        """查询答案。先精确匹配, 再(可选)模糊匹配。"""
        key = compact_text(question)
        if not key:
            return None
        with self._lock:
            self._maybe_reload()
            entry = self._exact.get(key)
            if entry:
                return entry["answer"]
            if not self.fuzzy:
                return None
            best = self._fuzzy_match(key, qtype)
            return best["answer"] if best else None

    def _fuzzy_match(self, key: str, qtype: Optional[str] = None) -> Optional[dict]:
        best: Optional[dict] = None
        best_score = 0.0
        max_len_gap = max(6, int(len(key) * 0.3))
        want_type = str(qtype).strip() if qtype else None
        for entry in self._entries:
            other = entry["key"]
            if not other:
                continue
            if abs(len(other) - len(key)) > max_len_gap:
                continue
            quick = SequenceMatcher(None, key, other).quick_ratio()
            if quick < self.threshold:
                continue
            score = SequenceMatcher(None, key, other).ratio()
            if want_type and entry.get("type") == want_type:
                score += 0.02  # 题型一致的轻微加权
            if score > best_score:
                best_score = score
                best = entry
        if best is not None and best_score >= self.threshold:
            return best
        return None

    # ----------------------------------------------------------------- write
    def add(self, question: Any, answer: Any, qtype: Any = None, source: Optional[str] = None) -> bool:
        """加入/更新一条题目(内存中), 返回是否发生变化。"""
        entry = _entry_from_question_answer(question, answer, qtype, source)
        if not entry:
            return False
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

    def merge(self, payload: Any, source: Optional[str] = None) -> int:
        """合并任意结构的一次性题库数据, 返回新增(不含答案变更)条数。"""
        added = 0
        for entry in collect_entries(payload, source=source):
            with self._lock:
                is_new = entry["key"] not in self._exact
            if self.add(entry["question"], entry["answer"], entry.get("type"), entry.get("source") or source):
                if is_new:
                    added += 1
        return added

    def import_cache(self, cache_path: Any = DEFAULT_CACHE_PATH, source: Optional[str] = None) -> int:
        """把答案缓存(cache.json: 题目 -> 答案)合并进题库。"""
        path = Path(cache_path)
        if not path.is_file():
            return 0
        try:
            payload = json.loads(_read_text(path) or "{}")
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return 0
        if not isinstance(payload, dict):
            return 0
        return self.merge(payload, source=source or f"cache:{path.name}")

    def save(self, path: Any = None, indent: int = 2) -> Optional[str]:
        """保存题库到文件(默认第一个路径), 返回写入的文件路径。

        按归一化后的题面去重(后加载/后写入的答案生效), 保证文件不会越存越乱。
        """
        target = Path(path) if path else (self.paths[0] if self.paths else Path(DEFAULT_BANK_PATH))
        with self._lock:
            # 按归一化题面去重: 后出现的条目覆盖同 key 的旧条目(dict 保留首次出现的位置)
            deduped: dict[str, dict] = {}
            for entry in self._entries:
                deduped[entry["key"]] = entry

            data: dict[str, Any] = {}
            for entry in deduped.values():
                if entry.get("type") or entry.get("source"):
                    item: dict[str, Any] = {"answer": entry["answer"]}
                    if entry.get("type"):
                        item["type"] = entry["type"]
                    if entry.get("source"):
                        item["source"] = entry["source"]
                    data[entry["question"]] = item
                else:
                    data[entry["question"]] = entry["answer"]
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    json.dumps(data, ensure_ascii=False, indent=indent) + "\n",
                    encoding="utf-8",
                )
            except OSError:
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


def merge_bank_files(inputs: Iterable[str | Path], output: str | Path, threshold: float = 0.86) -> dict:
    """把多个题库文件合并进一个题库文件(后加载的覆盖先加载的)。"""
    bank = LocalBank(paths=[str(p) for p in inputs], fuzzy=False, threshold=threshold, reload_interval=0)
    bank.load()
    written = bank.save(output)
    return {"entries": len(bank), "output": written}

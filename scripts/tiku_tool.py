#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地题库工具箱: 导入 / 合并 / 回写 / 查询自检。

把任何来源的题库(买的、别人给的、自己攒的)统一变成 ``tiku/bank.json``, 让脚本离线命中答案,
不再每题都去第三方题库付费查询。

常用命令::

    # 看一眼题库现状
    python scripts/tiku_tool.py stats --bank tiku/bank.json

    # 导入题库(支持 json / csv / txt, 格式自动识别)
    python scripts/tiku_tool.py import --bank tiku/bank.json --input 题库.json 题库.txt

    # 合并多个题库文件成一个
    python scripts/tiku_tool.py merge --bank tiku/bank.json --input a.json b.txt

    # 把这次运行攒下来的答案缓存合并进题库(越跑越准)
    python scripts/tiku_tool.py export --cache tiku/cache.json --bank tiku/bank.json

    # 自检: 某道题能不能命中
    python scripts/tiku_tool.py lookup --bank tiku/bank.json --question "以下哪个是网络协议？"

支持的文本题库行格式(分隔符任选其一, 自动识别)::

    题目内容----答案
    题目内容$$答案
    题目内容<TAB>答案
    题目内容,答案            (csv, 取第一列题目+最后一列答案)
    题目内容|答案|单选        (第三列可选: 题型)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.tiku_bank import (  # noqa: E402
    DEFAULT_BANK_PATH,
    DEFAULT_CACHE_PATH,
    LocalBank,
    compact_text,
    merge_bank_files,
    parse_text_entries,
    TEXT_TYPE_ALIASES,
)


def load_input(path: Path, fmt: str = "auto") -> tuple[list[dict] | dict | list, int]:
    """读取一个输入题库文件, 返回 (payload, 跳过行数)。"""
    if not path.is_file():
        print(f"[tiku_tool] 跳过不存在的文件: {path}", file=sys.stderr)
        return [], 0
    raw = path.read_text(encoding="utf-8", errors="ignore")
    use_json = fmt == "json" or (fmt == "auto" and path.suffix.lower() in {".json", ".jsonl"})
    if fmt == "auto" and not use_json and raw.lstrip().startswith(("{", "[")):
        use_json = True
    if use_json:
        try:
            return json.loads(raw), 0
        except json.JSONDecodeError as e:
            print(f"[tiku_tool] {path} 不是合法 JSON: {e}, 按文本格式重试", file=sys.stderr)
    entries, skipped = parse_text_entries(raw)
    return entries, skipped


def cmd_stats(args) -> int:
    bank = LocalBank(paths=args.bank, fuzzy=False, reload_interval=0)
    count = bank.load()
    info = bank.stats()
    print(f"题库文件: {', '.join(info['paths'])}")
    print(f"  实际加载文件: {', '.join(info['files']) or '(空)'}")
    print(f"  题目总数: {count}")
    cache = Path(args.cache)
    if cache.is_file():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8") or "{}")
            print(f"答案缓存: {cache} ({len(cached) if isinstance(cached, dict) else 0} 条)")
        except (json.JSONDecodeError, OSError):
            print(f"答案缓存: {cache} (无法解析)")
    else:
        print(f"答案缓存: {cache} (不存在)")
    return 0


def cmd_lookup(args) -> int:
    bank = LocalBank(paths=args.bank, fuzzy=not args.no_fuzzy, threshold=args.threshold, reload_interval=0)
    count = bank.load()
    print(f"[tiku_tool] 已加载 {count} 条题目")
    if not count:
        return 1
    answer = bank.lookup(args.question)
    key = compact_text(args.question)
    if answer:
        print(f"命中: {answer}")
        return 0
    print(f"未命中: {key[:60]}{'...' if len(key) > 60 else ''}")
    if args.add:
        answer_input = input("请手动输入该题答案(留空放弃): ").strip()
        if answer_input:
            bank.add(args.question, answer_input, source="manual")
            bank.save()
            print("已写入题库")
            return 0
    return 1


def cmd_import(args) -> int:
    bank = LocalBank(paths=args.bank, fuzzy=False, reload_interval=0)
    before = bank.load()
    total_added = 0
    total_skipped = 0
    for item in args.input:
        payload, skipped = load_input(Path(item), args.format)
        total_skipped += skipped
        added = bank.merge(payload, source=Path(item).name)
        total_added += added
        print(f"[tiku_tool] {item}: 新增 {added} 条")
    if args.dry_run:
        print(f"[tiku_tool] dry-run: 题库 {before} -> {before + total_added} 条(未写盘)")
        return 0
    output = bank.save(args.output or args.bank)
    print(f"[tiku_tool] 题库已保存: {output} ({before} -> {len(bank)} 条, 跳过 {total_skipped} 行)")
    return 0


def cmd_merge(args) -> int:
    result = merge_bank_files(args.input, args.bank, threshold=0.86)
    print(f"[tiku_tool] 合并完成: {result['entries']} 条 -> {result['output']}")
    return 0


def cmd_export(args) -> int:
    bank = LocalBank(paths=args.bank, fuzzy=False, reload_interval=0)
    before = bank.load()
    added = bank.import_cache(args.cache)
    print(f"[tiku_tool] 缓存 {args.cache} 已合并: 新增 {added} 条 (原 {before} 条)")
    if args.dry_run:
        print(f"[tiku_tool] dry-run: 未写盘, 合并后应为 {len(bank)} 条")
        return 0
    output = bank.save(args.output or args.bank)
    print(f"[tiku_tool] 题库已保存: {output} (共 {len(bank)} 条)")
    return 0


def cmd_dedupe(args) -> int:
    bank = LocalBank(paths=args.bank, fuzzy=False, reload_interval=0)
    before = bank.load()
    output = bank.save(args.output or args.bank)
    print(f"[tiku_tool] 去重/规整完成: {before} -> {len(bank)} 条 -> {output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    # --bank/--cache 用 parents 共享, 在子命令前后都能写; 未提供时解析后再补默认值
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--bank", default=argparse.SUPPRESS, help=f"题库文件(默认 {DEFAULT_BANK_PATH})")
    common.add_argument("--cache", default=argparse.SUPPRESS, help=f"答案缓存文件(默认 {DEFAULT_CACHE_PATH})")

    parser = argparse.ArgumentParser(description="本地题库工具", parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("stats", parents=[common], help="查看题库/缓存条数")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("lookup", parents=[common], help="查询一道题能否命中")
    p.add_argument("--question", "-q", required=True, help="题目内容")
    p.add_argument("--threshold", type=float, default=0.86, help="模糊匹配阈值")
    p.add_argument("--no-fuzzy", action="store_true", help="只用精确匹配")
    p.add_argument("--add", action="store_true", help="未命中时交互式补录答案")
    p.set_defaults(func=cmd_lookup)

    p = sub.add_parser("import", parents=[common], help="导入题库(json/csv/txt)")
    p.add_argument("--input", "-i", nargs="+", required=True, help="输入文件列表")
    p.add_argument("--format", "-f", choices=["auto", "json", "text"], default="auto")
    p.add_argument("--output", "-o", default=None, help="输出题库文件(默认覆盖 --bank)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("merge", parents=[common], help="合并多个题库文件")
    p.add_argument("--input", "-i", nargs="+", required=True, help="输入文件列表")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("export", parents=[common], help="把答案缓存合并回题库")
    p.add_argument("--output", "-o", default=None, help="输出题库文件(默认覆盖 --bank)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("dedupe", parents=[common], help="合并重复题目并规整格式")
    p.add_argument("--output", "-o", default=None)
    p.set_defaults(func=cmd_dedupe)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "bank", None):
        args.bank = DEFAULT_BANK_PATH
    if not getattr(args, "cache", None):
        args.cache = DEFAULT_CACHE_PATH
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从环境变量渲染 config.ini(GitHub Actions 用)。

为什么需要它:
    仓库里的 config.ini 被 .gitignore 忽略(里面是账号密码), 而 GitHub Actions 只能通过
    secrets 注入环境变量。这个脚本把环境变量渲染成一份完整的 config.ini, 同时**保留模板里的
    注释**, 这样 runner 上跑出来的配置依然可读、可核对。

用法::

    python scripts/gen_config.py --template config_template.ini --out config.ini --check
    python scripts/gen_config.py --env-file .env.local      # 本地调试

环境变量约定(前缀 CHAOXING_):
    CHAOXING_USERNAME / CHAOXING_PASSWORD / CHAOXING_COURSE_LIST / CHAOXING_SPEED /
    CHAOXING_JOBS / CHAOXING_NOTOPEN_ACTION / CHAOXING_VERBOSE
    CHAOXING_TIKU_XXX -> 写入 [tiku] 节的 xxx, 例如:
        CHAOXING_TIKU_PROVIDER=auto             -> provider=auto
        CHAOXING_TIKU_TOKENS=xxx,yyy            -> tokens=xxx,yyy
        CHAOXING_TIKU_SUBMIT=true               -> submit=true
        CHAOXING_TIKU_LOCAL_BANK=tiku/bank.json -> local_bank=tiku/bank.json
        CHAOXING_TIKU_LOCAL_API=http://127.0.0.1:8000/query
        CHAOXING_TIKU_GO_AUTHORIZATION=xxx
        CHAOXING_TIKU_ENDPOINT=https://xxx/v1  CHAOXING_TIKU_KEY=sk-xxx  CHAOXING_TIKU_MODEL=gpt-4o-mini
        CHAOXING_TIKU_CACHE_FILE=tiku/cache.json  CHAOXING_TIKU_EXPORT_BANK=true
        (别名: BANK/BANK_PATH->local_bank, CACHE->cache_file, THRESHOLD->local_threshold)
    CHAOXING_NOTIFY_PROVIDER / CHAOXING_NOTIFY_URL / CHAOXING_NOTIFY_TG_CHAT_ID
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.tiku_bank import collect_tiku_config_from_env  # noqa: E402

COMMON_ENV_MAP = {
    "CHAOXING_USERNAME": "username",
    "CHAOXING_PASSWORD": "password",
    "CHAOXING_COURSE_LIST": "course_list",
    "CHAOXING_SPEED": "speed",
    "CHAOXING_JOBS": "jobs",
    "CHAOXING_NOTOPEN_ACTION": "notopen_action",
    "CHAOXING_USE_COOKIES": "use_cookies",
    "CHAOXING_VERBOSE": "verbose",
}

NOTIFY_ENV_MAP = {
    "CHAOXING_NOTIFY_PROVIDER": "provider",
    "CHAOXING_NOTIFY_URL": "url",
    "CHAOXING_NOTIFY_TG_CHAT_ID": "tg_chat_id",
}

# 输出/日志里需要打码的键(Actions 日志可能是公开可见的, 手机号与各类 token 都不应出现)
SECRET_KEYS = {"username", "password", "tokens", "key", "siliconflow_key", "go_authorization",
               "url", "local_api"}

_HEADER_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_SETTING_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*=")


def parse_env_file(path: str | Path) -> dict:
    """读取 KEY=VALUE 形式的 .env 文件(支持 # 注释与引号)。"""
    env: dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return env
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        env[key.strip()] = value
    return env


def collect_values(env: dict) -> dict:
    """把环境变量整理成 {(section, key): value}。"""
    values: dict[tuple[str, str], str] = {}

    for env_key, conf_key in COMMON_ENV_MAP.items():
        raw = env.get(env_key)
        if raw is not None and str(raw).strip():
            values[("common", conf_key)] = str(raw).strip()

    for env_key, conf_key in NOTIFY_ENV_MAP.items():
        raw = env.get(env_key)
        if raw is not None and str(raw).strip():
            values[("notification", conf_key)] = str(raw).strip()

    for conf_key, value in collect_tiku_config_from_env(env).items():
        values[("tiku", conf_key)] = value

    return values


def split_sections(text: str) -> list[dict]:
    """把 ini 文本切成 [ {name, lines} ], name 为空表示文件开头的无节区。"""
    sections: list[dict] = []
    current = {"name": "", "lines": []}
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        match = _HEADER_RE.match(line)
        if match:
            sections.append(current)
            current = {"name": match.group(1).strip(), "lines": []}
        else:
            current["lines"].append(line)
    sections.append(current)
    return sections


def _render_section(section: dict) -> str:
    lines = list(section["lines"])
    # 去掉尾部多余的空白行, 统一由 join 补一个空行
    while lines and not lines[-1].strip():
        lines.pop()
    body = "\n".join(lines)
    if section["name"]:
        header = f"[{section['name']}]"
        return f"{header}\n{body}\n" if body else f"{header}\n"
    return f"{body}\n" if body else ""


def render_config(template_text: str, values: dict) -> str:
    """把 {(section, key): value} 渲染进 ini 模板, 保留原有注释与顺序。"""
    sections = split_sections(template_text)
    by_name = {}
    for section in sections:
        by_name.setdefault(section["name"], section)

    for (section_name, key), value in values.items():
        section = by_name.get(section_name)
        if section is None:
            section = {"name": section_name, "lines": []}
            sections.append(section)
            by_name[section_name] = section

        lines = section["lines"]
        new_line = f"{key} = {value}"
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=", re.IGNORECASE)

        replaced = False
        for idx, line in enumerate(lines):
            if pattern.match(line):
                indent = line[: len(line) - len(line.lstrip())]
                lines[idx] = f"{indent}{new_line}" if indent else new_line
                replaced = True
                break
        if replaced:
            continue

        insert_at = len(lines)
        while insert_at > 0 and not lines[insert_at - 1].strip():
            insert_at -= 1
        for idx in range(len(lines) - 1, -1, -1):
            if _SETTING_RE.match(lines[idx]):
                insert_at = idx + 1
                break
        else:
            insert_at = len(lines)
        lines.insert(insert_at, new_line)

    return "".join(_render_section(section) for section in sections)


def mask_value(key: str, value: str) -> str:
    if key not in SECRET_KEYS:
        return value
    text = str(value)
    if len(text) <= 4:
        return "****"
    return f"{text[:2]}****{text[-2:]}"


def default_template() -> str:
    return (
        "[common]\n"
        "use_cookies=false\n"
        "username =\n"
        "password =\n"
        "course_list =\n"
        "speed = 1\n"
        "jobs = 4\n"
        "notopen_action = retry\n"
        "\n"
        "[tiku]\n"
        "provider=TikuLocal\n"
        "submit=false\n"
        "cover_rate=0.9\n"
        "delay=1.0\n"
        "local_bank=tiku/bank.json\n"
        "cache_file=tiku/cache.json\n"
        "\n"
        "[notification]\n"
        "provider=ServerChan\n"
        "url=\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从环境变量渲染 config.ini")
    parser.add_argument("--template", default=str(REPO_ROOT / "config_template.ini"),
                        help="ini 模板(默认仓库根的 config_template.ini)")
    parser.add_argument("--out", default="config.ini", help="输出的 config.ini 路径")
    parser.add_argument("--env-file", default=None, help="可选: 额外加载一个 KEY=VALUE 文件(本地调试用)")
    parser.add_argument("--default-provider", default="auto",
                        help="环境变量没给 provider 时的默认值, 传空字符串则不写入(默认 auto)")
    parser.add_argument("--check", action="store_true", help="只打印将要写入的配置(敏感值打码), 不写文件")
    parser.add_argument("--print", dest="print_result", action="store_true", help="把渲染结果打印到 stdout")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    if args.env_file:
        env.update({k: v for k, v in parse_env_file(args.env_file).items() if v != ""})

    template_path = Path(args.template)
    template_text = template_path.read_text(encoding="utf-8") if template_path.is_file() else default_template()
    if not template_path.is_file():
        print(f"[gen_config] 模板不存在, 使用内置模板: {template_path}", file=sys.stderr)

    values = collect_values(env)
    if ("tiku", "provider") not in values and args.default_provider.strip():
        values[("tiku", "provider")] = args.default_provider.strip()

    if args.check:
        for section in ("common", "tiku", "notification"):
            items = {k: v for (sec, k), v in values.items() if sec == section}
            if not items:
                continue
            print(f"[gen_config] [{section}]")
            for key in sorted(items):
                print(f"    {key} = {mask_value(key, items[key])}")
        missing = [k for k in ("username", "password") if ("common", k) not in values]
        if missing:
            print(f"[gen_config] 提示: common 缺少 {', '.join(missing)} "
                  f"(若使用 cookies 登录可忽略)", file=sys.stderr)

    rendered = render_config(template_text, values)
    if args.print_result and not args.check:
        print(rendered)

    if args.check:
        return 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"[gen_config] 已写入 {out_path} ({len(values)} 项配置)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

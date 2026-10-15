#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""学习通扫码登录: 申请网页二维码 -> 打印成 ASCII(可直接用手机扫) -> 轮询扫码状态 -> 落地 cookies.txt

为什么需要它: 账号密码登录容易触发风控/冻结, 而且密码要一直存在 secrets 里。
扫码登录换来的会话 cookie 可以直接被主程序使用(`python main.py --use-cookies`)。

用法:
  python scripts/qr_login.py                     # 打印二维码并等待扫码(默认 300 秒)
  python scripts/qr_login.py --timeout 120       # 自己定等待时间
  python scripts/qr_login.py --no-ascii          # 不打印 ASCII(例如只看 png 文件)
  python scripts/qr_login.py --write-cookie "k=v;k2=v2"   # 把已有的 cookie 字符串落地成 cookies.txt
  python scripts/qr_login.py --print-cookie      # 成功后额外打印一行 cookie 串(本地跑时用来填 secret)

真实的超星接口(来自 passport2.chaoxing.com 登录页):
  1) GET  /login?fid=&newversion=true&refer=...  页面里带 uuid / enc
  2) GET  /createqr?uuid=<uuid>&fid=-1           二维码图片(PNG)
  3) POST /refreshQRCode                         换一张二维码(拿到新的 uuid/enc)
  4) POST /getauthstatus/v2                      轮询: status=true 表示已确认
                                                 type=4 已扫码待确认 / 6 已取消 / 7 出错
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys
import time
import pathlib
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

BASE = "https://passport2.chaoxing.com"
LOGIN_URL = (BASE + "/login?fid=&newversion=true&refer="
             + quote("https://i.chaoxing.com", safe=""))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# 主程序读的就是这个文件(api/config.py 的 GlobalConst.COOKIES_PATH), 扫码结果直接写进去
from api.config import GlobalConst as _gc          # noqa: E402
_COOKIE_PATH = _gc.COOKIES_PATH
COOKIE_FILE = (pathlib.Path(_COOKIE_PATH) if os.path.isabs(_COOKIE_PATH)
               else ROOT / _COOKIE_PATH)

INPUT_RE = {
    "uuid": (re.compile(r'id="uuid"[^>]*value="([^"]+)"'),
             re.compile(r'value="([^"]+)"[^>]*id="uuid"')),
    "enc": (re.compile(r'id="enc"[^>]*value="([^"]*)"'),
            re.compile(r'value="([^"]*)"[^>]*id="enc"')),
}


def parse_hidden(html: str, name: str) -> str:
    for pattern in INPUT_RE[name]:
        m = pattern.search(html)
        if m:
            return m.group(1).strip()
    return ""


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Referer": LOGIN_URL})
    return session


def fetch_qr(session: requests.Session) -> tuple[str, str, bytes]:
    """拿一张新二维码: 返回 (uuid, enc, png)。"""
    res = session.get(LOGIN_URL, timeout=20)
    res.raise_for_status()
    uuid, enc = parse_hidden(res.text, "uuid"), parse_hidden(res.text, "enc")
    if not uuid:
        refresh = session.post(BASE + "/refreshQRCode", timeout=20)
        data = refresh.json()
        uuid, enc = str(data.get("uuid", "")), str(data.get("enc", ""))
    if not uuid:
        raise RuntimeError("拿不到二维码 uuid(登录页结构可能变了)")
    png = session.get(f"{BASE}/createqr?uuid={uuid}&fid=-1", timeout=20).content
    if not png[:8] == b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("二维码接口没有返回 PNG")
    return uuid, enc, png


def ascii_qr(png: bytes, quiet: int = 2, max_cols: int = 60) -> str:
    """把二维码图片渲染成 ASCII(深色 '██', 浅色空格), 便于直接在日志里用手机扫。"""
    from PIL import Image

    image = Image.open(io.BytesIO(png)).convert("L")
    step = max(1, image.width // max_cols)        # 控制列数, 手机上才扫得动
    w, h = image.width // step, image.height // step
    px = image.load()

    def is_dark(x0: int, y0: int) -> bool:
        dark = 0
        for dy in range(step):
            for dx in range(step):
                if px[min(x0 * step + dx, image.width - 1),
                      min(y0 * step + dy, image.height - 1)] < 128:
                    dark += 1
        return dark * 2 > step * step

    pad = "  " * quiet
    rows = []
    for y in range(h):
        rows.append(pad + "".join("██" if is_dark(x, y) else "  " for x in range(w)) + pad)
    blank = "  " * (w + 2 * quiet)
    return "\n".join([blank] * quiet + rows + [blank] * quiet)


def poll_once(session: requests.Session, uuid: str, enc: str) -> dict:
    res = session.post(BASE + "/getauthstatus/v2",
                       data={"enc": enc, "uuid": uuid,
                             "doubleFactorLogin": "", "forbidotherlogin": ""},
                       timeout=20)
    try:
        return res.json()
    except ValueError:
        return {"status": False, "type": -1, "mes": "返回内容不是 JSON"}


def save_cookie_file(session: requests.Session) -> str:
    """把会话 cookie 写成主程序能读的一行格式(k=v;k=v)。"""
    text = ";".join(f"{k}={v}" for k, v in session.cookies.items())
    if not text:
        raise RuntimeError("扫码成功但没拿到 cookie")
    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_FILE.write_text(text, encoding="utf-8")
    return text


def write_cookie_string(raw: str) -> int:
    """把 cookie 字符串(通常来自 secret)落地成 cookies.txt, 供 --use-cookies 使用。"""
    pairs = []
    for item in raw.replace("\n", ";").split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key = item.split("=", 1)[0].strip()
        if key:
            pairs.append(item)
    if not pairs:
        print("[qr_login] cookie 字符串里没有解析出任何键值对, 请检查 secret 内容", file=sys.stderr)
        return 1
    COOKIE_FILE.write_text(";".join(pairs), encoding="utf-8")
    names = ", ".join(item.split("=", 1)[0] for item in pairs)
    print(f"[qr_login] 已写入 {COOKIE_FILE.name}({len(pairs)} 项: {names})")
    return 0


def qr_login(timeout: int, show_ascii: bool, print_cookie: bool) -> int:
    session = make_session()
    uuid, enc, png = fetch_qr(session)
    qr_path = ROOT / "qr.png"
    qr_path.write_bytes(png)
    print(f"[qr_login] 二维码已生成({len(png)} 字节), 也存到了 {qr_path.name}")
    if show_ascii:
        print("\n请用【学习通 App -> 右上角扫一扫】扫下面这张码(日志里可实时扫):\n")
        print(ascii_qr(png))
        print("\n扫完请在手机上点【确认登录】。等待中...(最多 %d 秒)" % timeout)

    deadline = time.time() + timeout
    last_note = ""
    scanned = False
    while time.time() < deadline:
        data = poll_once(session, uuid, enc)
        if data.get("status"):
            print("[qr_login] 手机已确认, 登录成功 ✓")
            cookie_text = save_cookie_file(session)
            names = ", ".join(k for k, _ in session.cookies.items())
            print(f"[qr_login] 已写入 {COOKIE_FILE.name}({len(session.cookies)} 项: {names})")
            print("[qr_login] 接下来可以: python main.py --use-cookies ...")
            if print_cookie:
                print("[qr_login] cookie 字符串(本地跑时把它存进 secret CHAOXING_COOKIE; "
                      "在公开仓库的 Actions 日志里不要开这个开关):")
                print(cookie_text)
            return 0

        kind = data.get("type")
        if kind == 4 and not scanned:
            scanned = True
            nickname = data.get("nickname", "")
            print(f"[qr_login] 已扫码({nickname}), 请在手机上点确认…")
        elif kind == 6:
            print("[qr_login] 手机上取消了, 重新生成二维码…")
            uuid, enc, png = fetch_qr(session)
            qr_path.write_bytes(png)
            if show_ascii:
                print(ascii_qr(png))
            print("[qr_login] 请重新扫码(最多 %d 秒)" % max(0, int(deadline - time.time())))
        elif kind == 7:
            note = str(data.get("mes", ""))
            if note != last_note:
                print(f"[qr_login] 服务端提示: {note}, 重新生成二维码…")
                last_note = note
            uuid, enc, png = fetch_qr(session)
            qr_path.write_bytes(png)
            if show_ascii:
                print(ascii_qr(png))
        time.sleep(2)

    print("[qr_login] 等待超时: 没有检测到扫码确认。可以重跑脚本, 或改用账号密码登录。",
          file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="学习通扫码登录(cookie 落地)")
    parser.add_argument("--timeout", type=int, default=300, help="等待扫码的秒数, 默认 300")
    parser.add_argument("--no-ascii", action="store_true", help="不打印 ASCII 二维码")
    parser.add_argument("--print-cookie", action="store_true",
                        help="成功后打印 cookie 字符串(建议只在本地跑时使用)")
    parser.add_argument("--write-cookie", metavar="COOKIE",
                        help="不扫码, 直接把 cookie 字符串写入 cookies.txt")
    args = parser.parse_args()

    if args.write_cookie:
        return write_cookie_string(args.write_cookie)
    if os.environ.get("CHAOXING_COOKIE") and not args.print_cookie:
        # 没显式要求扫码时, 若环境里已经给了 cookie, 直接落地更省事
        return write_cookie_string(os.environ["CHAOXING_COOKIE"])
    try:
        return qr_login(max(10, args.timeout), not args.no_ascii, args.print_cookie)
    except Exception as exc:                       # noqa: BLE001
        print(f"[qr_login] 失败: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

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
import json
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


# 依次尝试, 第一个成功的就用。顺序按"国内能不能打开"实测排的:
#   uguu.se      实测国内可打开 ✓(首选)
#   tmpfiles.org 国内打开返回的不是图片(留作备用)
#   catbox.moe   国内被墙(备用)
#   0x0.st       已关闭上传(移除)
UPLOAD_TARGETS = (
    ("uguu.se", lambda session, png: _upload_uguu(session, png)),
    ("tmpfiles.org", lambda session, png: _upload_tmpfiles(session, png)),
    ("catbox.moe", lambda session, png: _upload_catbox(session, png)),
)


def _upload_tmpfiles(session: requests.Session, png: bytes) -> str:
    res = session.post("https://tmpfiles.org/api/v1/upload",
                       files={"file": ("qr.png", png, "image/png")}, timeout=40)
    url = res.json()["data"]["url"]
    return url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)


def _upload_0x0(session: requests.Session, png: bytes) -> str:
    res = session.post("https://0x0.st", files={"file": ("qr.png", png, "image/png")},
                       timeout=40)
    return res.text.strip()


def _upload_catbox(session: requests.Session, png: bytes) -> str:
    res = session.post("https://catbox.moe/user/api.php",
                       data={"reqtype": "fileupload"},
                       files={"fileToUpload": ("qr.png", png, "image/png")}, timeout=40)
    return res.text.strip()


def _upload_uguu(session: requests.Session, png: bytes) -> str:
    res = session.post("https://uguu.se/upload", params={"output": "text"},
                       files={"files[]": ("qr.png", png, "image/png")}, timeout=40)
    return res.text.strip()


def upload_qr(png: bytes) -> str:
    """把二维码传到匿名图床, 返回可点开的直链(全部失败时返回空串)。"""
    session = make_session()
    for name, uploader in UPLOAD_TARGETS:
        try:
            url = uploader(session, png)
            if url.startswith("http"):
                print(f"[qr_login] 二维码图片已上传到 {name}(在电脑上打开下面这个链接, 用手机扫屏幕):")
                print(f"[qr_login] {url}")
                return url
        except Exception as exc:                       # noqa: BLE001
            print(f"[qr_login] 上传到 {name} 失败({exc}), 换下一个图床…")
    print("[qr_login] 所有图床都不可用: 请看下面的 ASCII 二维码, 或等运行结束后下载 qr.png")
    return ""


def publish_qr(url: str, png_path: Path) -> None:
    """把二维码链接/图片写进运行摘要(手机上打开运行页面就能直接扫)。"""
    if url:
        print(f"\n二维码图片(点开就能扫): {url}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    try:
        with open(summary, "a", encoding="utf-8") as fp:
            fp.write("## 扫码登录\n\n")
            if url:
                fp.write(f"[点不开就复制这个链接]({url})\n\n![二维码]({url})\n\n")
                fp.write("> 用【学习通 App → 右上角扫一扫】扫上面这张码, 然后在手机上点【确认登录】。\n")
                fp.write("> 注意: 这张码是**一次性**的, 登录成功后就没用了; 超时会自动换新码。\n\n")
            else:
                fp.write("二维码上传失败, 请看这次运行的日志里的 ASCII 码, "
                         "或等运行结束下载 `qr.png`。\n\n")
            fp.write(f"本地也保存了一份: `{png_path.name}`\n")
    except OSError as exc:
        print(f"[qr_login] 写运行摘要失败: {exc}")


STATE_FILE = ROOT / "qr_state.json"


def save_state(session: requests.Session, uuid: str, enc: str) -> None:
    STATE_FILE.write_text(json.dumps({
        "uuid": uuid, "enc": enc,
        "cookies": session.cookies.get_dict(),
        "saved_at": time.time(),
    }, ensure_ascii=False), encoding="utf-8")


def load_state():
    if not STATE_FILE.exists():
        raise RuntimeError(f"找不到 {STATE_FILE.name}, 请先跑一次 --prepare")
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    session = make_session()
    session.cookies.update(data.get("cookies") or {})
    return session, data["uuid"], data["enc"]


def prepare(timeout_note: str, upload: bool, show_ascii: bool) -> int:
    """步骤A: 只负责把二维码弄出来并让人看到, 立刻结束。"""
    session = make_session()
    uuid, enc, png = fetch_qr(session)
    qr_path = ROOT / "qr.png"
    qr_path.write_bytes(png)
    save_state(session, uuid, enc)
    print(f"[qr_login] 二维码已生成({len(png)} 字节) -> {qr_path.name}; 状态已存 {STATE_FILE.name}")
    url = upload_qr(png) if upload else ""
    publish_qr(url, qr_path)
    if show_ascii and not url:
        print(ascii_qr(png))
    print(f"[qr_login] 用【学习通 App -> 右上角扫一扫】扫码, 然后点手机上的【确认登录】"
          f"({timeout_note})")
    return 0


def wait_for_scan(timeout: int, upload: bool, show_ascii: bool, print_cookie: bool) -> int:
    """步骤B: 等着扫码确认, 成功后落地 cookies.txt。"""
    session, uuid, enc = load_state()
    deadline = time.time() + timeout
    scanned = False
    last_note = ""
    while time.time() < deadline:
        data = poll_once(session, uuid, enc)
        if data.get("status"):
            cookie_text = save_cookie_file(session)
            names = ", ".join(k for k, _ in session.cookies.items())
            print(f"[qr_login] 手机已确认, 登录成功 ✓ 已写入 {COOKIE_FILE.name}({len(session.cookies)} 项: {names})")
            if print_cookie:
                print("[qr_login] cookie 字符串:")
                print(cookie_text)
            STATE_FILE.unlink(missing_ok=True)
            return 0
        kind = data.get("type")
        if kind == 4 and not scanned:
            scanned = True
            print(f"[qr_login] 已扫码({data.get('nickname', '')}), 请在手机上点【确认登录】…")
        elif kind in (6, 7):
            note = str(data.get("mes", "")) if kind == 7 else "手机取消了"
            if note != last_note:
                print(f"[qr_login] {note}, 换成新二维码…")
                last_note = note
            uuid, enc, png = fetch_qr(session)
            (ROOT / "qr.png").write_bytes(png)
            save_state(session, uuid, enc)
            url = upload_qr(png) if upload else ""
            if url:
                print(f"[qr_login] 新二维码链接(点开就能扫): {url}")
            if show_ascii:
                print(ascii_qr(png))
        time.sleep(2)
    print("[qr_login] 等待超时: 没有检测到扫码确认。可以重跑这一步, 或改用账号密码登录。",
          file=sys.stderr)
    return 1


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


def qr_login(timeout: int, show_ascii: bool, print_cookie: bool, upload: bool = False) -> int:
    session = make_session()
    uuid, enc, png = fetch_qr(session)
    qr_path = ROOT / "qr.png"
    qr_path.write_bytes(png)
    print(f"[qr_login] 二维码已生成({len(png)} 字节), 也存到了 {qr_path.name}")
    url = upload_qr(png) if upload else ""
    publish_qr(url, qr_path)
    if show_ascii:
        print("\n请用【学习通 App -> 右上角扫一扫】扫描二维码图片(上面的链接/运行摘要里的图片最方便):\n")
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
            url = upload_qr(png) if upload else ""
            publish_qr(url, qr_path)
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
            url = upload_qr(png) if upload else ""
            publish_qr(url, qr_path)
            if show_ascii:
                print(ascii_qr(png))
        time.sleep(2)

    print("[qr_login] 等待超时: 没有检测到扫码确认。可以重跑脚本, 或改用账号密码登录。",
          file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="学习通扫码登录(cookie 落地)")
    parser.add_argument("--timeout", type=int, default=300, help="等待扫码的秒数, 默认 300")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true",
                      help="只生成二维码/上传/写运行摘要, 立刻结束(供 CI 分两步使用)")
    mode.add_argument("--wait", action="store_true",
                      help="读取上一次 --prepare 的状态, 等待扫码确认并落地 cookies.txt")
    parser.add_argument("--no-ascii", action="store_true", help="不打印 ASCII 二维码")
    parser.add_argument("--print-cookie", action="store_true",
                        help="成功后打印 cookie 字符串(建议只在本地跑时使用)")
    parser.add_argument("--write-cookie", metavar="COOKIE",
                        help="不扫码, 直接把 cookie 字符串写入 cookies.txt")
    parser.add_argument("--no-upload", action="store_true",
                        help="不把二维码上传到匿名图床(默认上传, 方便在运行摘要里直接扫)")
    args = parser.parse_args()

    if args.write_cookie:
        return write_cookie_string(args.write_cookie)
    if args.prepare:
        try:
            return prepare(f"最多 {args.timeout} 秒内有效", not args.no_upload, not args.no_ascii)
        except Exception as exc:                       # noqa: BLE001
            print(f"[qr_login] 生成二维码失败: {exc}", file=sys.stderr)
            return 2
    if args.wait:
        try:
            return wait_for_scan(max(10, args.timeout), not args.no_upload,
                                 not args.no_ascii, args.print_cookie)
        except Exception as exc:                       # noqa: BLE001
            print(f"[qr_login] 等待扫码失败: {exc}", file=sys.stderr)
            return 2
    if os.environ.get("CHAOXING_COOKIE") and not args.print_cookie:
        # 没显式要求扫码时, 若环境里已经给了 cookie, 直接落地更省事
        return write_cookie_string(os.environ["CHAOXING_COOKIE"])
    try:
        return qr_login(max(10, args.timeout), not args.no_ascii, args.print_cookie,
                        upload=not args.no_upload)
    except Exception as exc:                       # noqa: BLE001
        print(f"[qr_login] 失败: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

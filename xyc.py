#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Super Flash Download连接器 xyc —— 命令行客户端，驱动Super Flash Download GUI 的本机 REST API。

用法:
    xyc list [--json]                 列出全部任务（状态/进度/速度）
    xyc add <url> [-o 目录] [-n 线程] [--limit KB/s] [--json]
                                      推送任务（http/magnet/.torrent 自动识别）
    xyc info <tid> [--json]           单任务详情
    xyc pause <tid|all>               暂停
    xyc resume <tid|all>              继续（中断的任务会自动续传）
    xyc delete <tid|all> [--with-file]  删除记录（--with-file 连文件一起删）
    xyc health                        GUI 在线状态

GUI 未运行时自动拉起桌面客户端并等待 API 就绪（无需手动点图标）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

PORT = 16901
BASE = f"http://127.0.0.1:{PORT}"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHONW = os.path.join(APP_DIR, "venv", "Scripts", "pythonw.exe")
GUI_SCRIPT = os.path.join(APP_DIR, "xygui.py")
LAUNCH_TIMEOUT = 25.0


def _try_req(method: str, path: str, body: dict | None = None):
    """发一次请求；连接失败返回 None（不抛），其它异常照旧。"""
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except (urllib.error.URLError, ConnectionError, OSError):
        return None


def _gui_running() -> bool:
    """API 真能返回 JSON 才算就绪（单看端口会撞上释放瞬间的竞态）。"""
    return _try_req("GET", "/health") is not None


def _launch_gui() -> bool:
    """拉起桌面客户端（pythonw 无控制台窗口）。返回是否成功启动进程。"""
    if not os.path.exists(PYTHONW) or not os.path.exists(GUI_SCRIPT):
        return False
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        subprocess.Popen([PYTHONW, GUI_SCRIPT], cwd=APP_DIR,
                         creationflags=flags, close_fds=True)
        return True
    except Exception:
        return False


def ensure_gui(auto_start: bool = True) -> bool:
    """确保 GUI 在跑。返回 True 表示 API 可用。"""
    if _gui_running():
        return True
    if not auto_start:
        return False
    if not _launch_gui():
        print("Super Flash Download GUI 未运行，且自动启动失败（检查 venv/pythonw.exe 与 xygui.py）",
              file=sys.stderr)
        return False
    deadline = time.time() + LAUNCH_TIMEOUT
    while time.time() < deadline:
        time.sleep(0.4)
        if _gui_running():
            return True
    print(f"Super Flash Download GUI 启动超时（{LAUNCH_TIMEOUT:.0f}s 内 API 未就绪）", file=sys.stderr)
    return False


def req(method: str, path: str, body: dict | None = None) -> dict:
    d = _try_req(method, path, body)
    if d is None:
        print("Super Flash Download GUI 未运行（需先启动桌面客户端）", file=sys.stderr)
        sys.exit(2)
    return d


def fmt_size(b) -> str:
    try:
        b = float(b)
    except (TypeError, ValueError):
        return "—"
    for unit, div in (("GB", 1073741824), ("MB", 1048576), ("KB", 1024)):
        if b >= div:
            return f"{b / div:.1f}{unit}"
    return f"{b:.0f}B"


def print_table(tasks: list[dict]) -> None:
    if not tasks:
        print("（无任务）")
        return
    hdr = f'{"tid":>4}  {"状态":<8} {"进度":>5} {"大小":>12} {"速度":>10}  名称'
    print(hdr)
    print("-" * len(hdr.expandtabs()))
    for t in tasks:
        name = t["name"] if len(t["name"]) <= 38 else t["name"][:35] + "..."
        print(f'{t["tid"]:>4}  {t["state_label"]:<8} {t["pct"]:>4}% '
              f'{t["size_str"]:>12} {t["speed"]:>10}  {name}')


def resolve_tids(arg: str, tasks: list[dict]) -> list[int]:
    if arg == "all":
        return [t["tid"] for t in tasks]
    try:
        return [int(arg)]
    except ValueError:
        print(f"无效任务 ID: {arg}", file=sys.stderr)
        sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser(description="Super Flash Download连接器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--json", action="store_true")

    p_add = sub.add_parser("add")
    p_add.add_argument("url")
    p_add.add_argument("-o", "--out", default=None, help="保存目录")
    p_add.add_argument("-n", "--threads", type=int, default=16)
    p_add.add_argument("--limit", type=int, default=0, help="限速 KB/s，0 不限")
    p_add.add_argument("--proxy", default=None,
                       help="代理，例 http://127.0.0.1:7890（留空＝跟随系统代理）")
    p_add.add_argument("--mirror", action="append", default=None,
                       help="镜像 URL，可重复；HF 官方链接会自动附 hf-mirror")
    p_add.add_argument("--auto-name", action="store_true",
                       help="文件名取服务器 Content-Disposition（URL 末段是占位符时用）")

    p_info = sub.add_parser("info")
    p_info.add_argument("tid")
    p_info.add_argument("--json", action="store_true")

    for name in ("pause", "resume", "delete"):
        p = sub.add_parser(name)
        p.add_argument("tid", help="任务 ID 或 all")
        if name == "delete":
            p.add_argument("--with-file", action="store_true")

    sub.add_parser("health")

    a = ap.parse_args()

    # GUI 未运行就自动拉起（health 是纯探活，不拉起）
    if a.cmd != "health" and not ensure_gui():
        return 2

    if a.cmd == "health":
        d = _try_req("GET", "/health")
        if d is None:
            print(json.dumps({"ok": False, "error": "Super Flash Download GUI 未运行"}, ensure_ascii=False))
            return 2
        print(json.dumps(d, ensure_ascii=False))
        return 0

    if a.cmd == "list":
        d = req("GET", "/tasks")
        if a.json:
            print(json.dumps(d, ensure_ascii=False, indent=1))
        else:
            print_table(d.get("tasks", []))
        return 0

    if a.cmd == "add":
        body = {"url": a.url, "threads": a.threads, "limit": a.limit * 1024}
        if a.out:
            body["dest"] = a.out
        if a.proxy:
            body["proxy"] = a.proxy
        if a.mirror:
            body["mirrors"] = a.mirror
        if a.auto_name:
            body["auto_name"] = True
        d = req("POST", "/tasks", body)
        if d.get("ok"):
            print(f"已推送到Super Flash Download：{d.get('name', a.url)}")
            print("（GUI 任务列表实时可见，用 xyc list 查进度）")
            return 0
        print(f"失败: {d.get('error')}", file=sys.stderr)
        return 1

    if a.cmd == "info":
        d = req("GET", "/tasks")
        t = next((x for x in d.get("tasks", []) if x["tid"] == int(a.tid)), None)
        if t is None:
            print(f"任务 {a.tid} 不存在", file=sys.stderr)
            return 1
        if a.json:
            print(json.dumps(t, ensure_ascii=False, indent=1))
            return 0
        print(f"任务 {t['tid']}  {t['name']}")
        print(f"  状态: {t.get('state_label', t['state'])}"
              f"   进度: {t.get('pct', 0)}%"
              f"   大小: {t.get('size_str', '—')}"
              f"   速度: {t.get('speed', '') or '—'}")
        print(f"  落点: {t.get('dest', '')}")
        if t.get("err"):
            print(f"  错误: {t['err']}")
        return 0

    tasks = req("GET", "/tasks").get("tasks", [])
    tids = resolve_tids(a.tid, tasks)
    rc = 0
    for tid in tids:
        if a.cmd == "pause":
            d = req("POST", f"/tasks/{tid}/pause")
        elif a.cmd == "resume":
            d = req("POST", f"/tasks/{tid}/resume")
        else:
            d = req("DELETE", f"/tasks/{tid}" + ("?with_file=1" if a.with_file else ""))
        ok = d.get("ok")
        print(f"{tid}: {'OK' if ok else d.get('error', '失败')}")
        rc |= 0 if ok else 1
    return rc


if __name__ == "__main__":
    sys.exit(main())

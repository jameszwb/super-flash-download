#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Super Flash Download xy —— HTTP 多线程分片下载器（标准库实现，零第三方依赖）

用法:
    python xy.py <url> [-o 输出文件] [-n 线程数] [-s 分片大小MB] [-H "K: V"] [-r 重试]
    python xy.py <url.txt> ...          # 批量，每行一个 URL
断点续传：中断后重跑同一命令，自动从 .sfd 状态文件续传。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) xy/1.0"
TIMEOUT = 30
CHUNK = 256 * 1024


# ---------- 状态文件（断点续传） ----------
def state_path(path: str) -> str:
    return path + ".sfd"


def load_state(path: str, url: str, size: int) -> dict:
    try:
        with open(state_path(path), "r", encoding="utf-8") as f:
            st = json.load(f)
        if st.get("url") == url and st.get("size") == size:
            return st
    except Exception:
        pass
    return {"url": url, "size": size, "done": []}


def save_state(path: str, st: dict) -> None:
    tmp = state_path(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, state_path(path))


# ---------- HTTP ----------
def probe(url: str, headers: dict[str, str]) -> tuple[int, bool, str]:
    """返回 (总大小, 是否支持断点, 建议文件名)。不支持 Range 时 size=0。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, **headers}, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            size = int(r.headers.get("Content-Length") or 0)
            ranges = (r.headers.get("Accept-Ranges") or "").lower() == "bytes"
            name = os.path.basename(url.split("?")[0]) or "download"
            return size, ranges, name
    except Exception:
        pass
    req = urllib.request.Request(url, headers={"User-Agent": UA, **headers})
    req.add_header("Range", "bytes=0-")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        size = int(r.headers.get("Content-Range", "0/0").split("/")[-1] or 0)
        ranges = r.status == 206
        name = os.path.basename(url.split("?")[0]) or "download"
        return size, ranges, name


def split_ranges(size: int, parts: int) -> list[tuple[int, int]]:
    if size <= 0 or parts <= 1:
        return [(0, size - 1)]
    step = max(CHUNK, size // parts)
    out, start = [], 0
    while start < size:
        end = min(start + step, size) - 1
        out.append((start, end))
        start = end + 1
    return out


# ---------- 下载 ----------
class Progress:
    def __init__(self, total: int):
        self.total = total
        self.done = 0
        self.lock = threading.Lock()
        self.start = time.time()

    def add(self, n: int) -> None:
        with self.lock:
            self.done += n
            self.render()

    def render(self) -> None:
        if not sys.stdout.isatty():
            return
        el = max(time.time() - self.start, 1e-6)
        pct = self.done * 100 / self.total if self.total else 0
        bar = "#" * int(pct / 2)
        sys.stdout.write(
            f"\r[{bar:<50}] {pct:5.1f}%  {self.done / 1048576:8.1f}MB  "
            f"{self.done / el / 1048576:5.2f}MB/s  {el:5.0f}s"
        )
        sys.stdout.flush()


def fetch_piece(url: str, dest: str, start: int, end: int, headers, retries, prog) -> tuple[int, int]:
    """下载 [start,end] 到 dest 的正确偏移。返回 (start, end)。"""
    pos = start
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, **headers})
            if end >= 0:
                req.add_header("Range", f"bytes={pos}-{end}")
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(dest, "r+b") as f:
                f.seek(pos)
                while True:
                    buf = r.read(CHUNK)
                    if not buf:
                        break
                    f.write(buf)
                    pos += len(buf)
                    prog.add(len(buf))
            return start, end
        except Exception as e:
            if attempt >= retries:
                raise
            time.sleep(1.5 * (attempt + 1))
            print(f"\n  [重试 {attempt+1}/{retries}] {start}-{end}: {e}", file=sys.stderr)
    return start, end


def download(url: str, out: str | None, threads: int, part_mb: int, headers: dict[str, str],
             retries: int) -> str:
    size, ranges, name = probe(url, headers)
    dest = out or name
    if os.path.dirname(dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)

    if size <= 0 or not ranges:
        # 不支持 Range：单线程流式下，边下边起
        print(f"[单线程] {url} -> {dest}（服务端不支持断点续传）")
        prog = Progress(1)
        req = urllib.request.Request(url, headers={"User-Agent": UA, **headers})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            prog.total = total or 1
            while True:
                buf = r.read(CHUNK)
                if not buf:
                    break
                f.write(buf)
                prog.add(len(buf))
        prog.render()
        print()
        return dest

    st = load_state(dest, url, size)
    if not os.path.exists(dest) or os.path.getsize(dest) != size:
        with open(dest, "wb") as f:
            f.truncate(size)
        st["done"] = []
    done_ranges = {tuple(x) for x in st["done"]}

    parts = [(s, e) for s, e in split_ranges(size, threads) if (s, e) not in done_ranges]
    if not parts:
        print(f"[已完成] {dest} ({size/1048576:.1f}MB)")
        return dest

    print(f"[分片] {dest}  {size/1048576:.1f}MB  {len(parts)} 片 x {part_mb}MB  "
          f"续传进度 {len(done_ranges)}/{len(split_ranges(size, threads))}")
    prog = Progress(size)
    prog.done = sum(e - s + 1 for s, e in done_ranges)
    prog.render()

    errs: list[Exception] = []
    lock = threading.Lock()
    sem = threading.Semaphore(threads)

    def worker(s: int, e: int) -> None:
        with sem:
            try:
                fetch_piece(url, dest, s, e, headers, retries, prog)
                with lock:
                    st["done"].append([s, e])
                    save_state(dest, st)
            except Exception as ex:
                with lock:
                    errs.append(ex)

    ts = [threading.Thread(target=worker, args=p, daemon=True) for p in parts]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    if errs:
        raise errs[0]
    if os.path.exists(state_path(dest)):
        os.remove(state_path(dest))
    print(f"\n[完成] {dest}")
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description="Super Flash Download · HTTP 多线程分片下载器")
    ap.add_argument("url", help="URL，或每行一个 URL 的 .txt 批量文件")
    ap.add_argument("-o", "--out", help="输出文件（单 URL 模式）")
    ap.add_argument("-n", "--threads", type=int, default=8, help="线程数，默认 8")
    ap.add_argument("-s", "--part", type=int, default=32, help="每片大小 MB，默认 32")
    ap.add_argument("-H", "--header", action="append", default=[], help='请求头 "K: V"，可多次')
    ap.add_argument("-r", "--retries", type=int, default=3, help="单片重试次数，默认 3")
    a = ap.parse_args()

    headers = dict(h.split(":", 1) for h in a.header if ":" in h)
    headers = {k.strip(): v.strip() for k, v in headers.items()}

    urls = []
    if os.path.isfile(a.url):
        with open(a.url, encoding="utf-8") as f:
            urls = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    else:
        urls = [a.url]

    rc = 0
    for i, u in enumerate(urls, 1):
        print(f"\n=== [{i}/{len(urls)}] {u}")
        try:
            download(u, a.out if len(urls) == 1 else None, a.threads, a.part, headers, a.retries)
        except Exception as e:
            print(f"[失败] {u}: {e}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())

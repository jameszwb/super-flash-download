#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Super Flash Download引擎 xyengine —— work-stealing 分片下载核心（纯标准库，可独立 CLI 使用）

对标并超越迅雷 HTTP 引擎的两个关键点：
1. work-stealing 动态分片：块队列共享，快线程自动偷慢线程的活，无静态片旱涝不均
2. HTTP keep-alive 连接池：每线程持长连接，零重复握手

功能：多线程 / 断点续传 / 暂停恢复 / 限速 / 代理 / Cookie / 批量 / 校验
"""
from __future__ import annotations

import http.client
import base64
import json
import os
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")
# UA 必须是像真实浏览器的：部分下载站（部分网盘）只对带 AppleWebKit/Gecko
# 的 UA 返回 RFC 5987 编码的 Content-Disposition，自造 UA 会让服务端把中文
# 文件名替换成空格（不可恢复，落盘变成 "CAD      .rar"）。
CHUNK = 256 * 1024          # 磁盘写粒度
# work-stealing 分配粒度。实测 1MB 块 → 6.0MB/s，8MB 块 → 21.2MB/s：
# 块越小，每次换块都要重新 302 + Range 请求，握手/请求开销吃掉大部分带宽。
STEAL_CHUNK = 8 * 1024 * 1024
# 尾部小块：剩余量 < 4MB 时改用 256KB，避免最后一块被单线程独占后
# 卡在低带宽连接上（实测出现过 0.6MB 停在 6KB/s 十几分钟）。
TAIL_PIECE = 256 * 1024
CONNECT_TIMEOUT = 30
ssl_ctx = ssl.create_default_context()
ssl_ctx_legacy = ssl.create_default_context()
try:
    ssl_ctx_legacy.set_ciphers("DEFAULT:@SECLEVEL=1")   # 兼容 DH 密钥过短的旧服务器
except Exception:
    pass


# ============ 连接地址族：IPv4 优先 ============
# 本机 IPv6 出口对境外 CDN（Cloudflare 系）有 PMTU 黑洞：TCP 握手（小包）能通，
# TLS 握手（大包）被静默丢弃 → 表现为「connect 成功但 ssl handshake 超时」。
# CPython 的 socket.create_connection 按 getaddrinfo 原序依次尝试，而 Windows 上
# AAAA 记录通常排在 A 记录前面 → 每个连接都先撞进黑洞再回落，20~30s 才超时。
# curl 有 happy-eyeballs 双栈竞速，所以同一 URL curl 正常、Python 超时。
#
# 修法（共享层，勿在各调用点各加 guard）：包一层 socket.getaddrinfo，把 AF_INET
# 提到前面。IPv6 仍保留在后面做回落，纯 IPv6 站点不会被打死。
# 影响面：本进程内所有走 getaddrinfo 的连接（http.client / urllib / socket）。
# 纯函数 _prefer_ipv4 便于单测。


def _prefer_ipv4(infos: list) -> list:
    """把 AF_INET 排到 AF_INET6 前面（稳定排序，族内原序不变）。"""
    return sorted(infos, key=lambda ai: 0 if ai[0] == socket.AF_INET else 1)


_orig_getaddrinfo = socket.getaddrinfo


def _getaddrinfo_ipv4_first(host, port, family=0, type=0, proto=0, flags=0):
    infos = _orig_getaddrinfo(host, port, family, type, proto, flags)
    return _prefer_ipv4(infos)


socket.getaddrinfo = _getaddrinfo_ipv4_first


def _is_legacy_ssl_error(e: Exception) -> bool:
    """SSL 协商类错误（老服务器 DH/RSA 太弱），值得用低安全等级重试。"""
    s = str(e).lower()
    return ("dh_key_too_small" in s or "dh key too small" in s
            or "key too small" in s or "ssl" in s and "handshake" in s)


# ============ 状态持久化 ============
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


def split_ranges(size: int, parts: int) -> list[tuple[int, int]]:
    """静态切分（初始化用）；work-stealing 模式下只是参考值。"""
    if size <= 0 or parts <= 1:
        return [(0, size - 1)]
    step = max(CHUNK, size // parts)
    out, start = [], 0
    while start < size:
        end = min(start + step, size) - 1
        out.append((start, end))
        start = end + 1
    return out


# ============ 探测 ============
def _open_compat(url: str, req: urllib.request.Request, timeout: int = 30,
                 proxy: str | None = None):
    """open with SSL 兼容降级：默认上下文握手失败（老服务器 DH 太弱等）
    时自动用 SECLEVEL=1 上下文重试一次。"""
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler(
            {"http": proxy, "https": proxy}))
    try:
        opener = urllib.request.build_opener(*handlers)
        return opener.open(req, timeout=timeout)
    except urllib.error.URLError as e:
        if not _is_legacy_ssl_error(e):
            raise
        handlers.append(urllib.request.HTTPSHandler(context=ssl_ctx_legacy))
        opener = urllib.request.build_opener(*handlers)
        # Request 对象可复用（urlopen 不消费它）
        return opener.open(req, timeout=timeout)


def filename_from_headers(headers) -> str:
    """从响应头解析真实文件名。

    共享层：GUI / CLI / 落盘都走这里，避免各处各写一套解析。
    支持 RFC 5987 ``filename*=UTF-8''%E4%B8%AD%E6%96%87.txt``（百分号编码，
    网盘/下载站的标准写法），以及普通 ``filename="x.txt"``。
    """
    cd = (headers.get("Content-Disposition") or "") if headers else ""
    if not cd:
        return ""
    if "filename*=" in cd:
        val = cd.split("filename*=", 1)[1].strip()
        # charset'lang'value —— 语言段可为空，只按前两个单引号切
        parts = val.split("'", 2)
        raw = parts[2] if len(parts) == 3 else val
        try:
            return os.path.basename(urllib.parse.unquote(raw.strip().strip('"')))
        except Exception:
            return ""
    if "filename=" in cd:
        fn = cd.split("filename=", 1)[1].strip().strip('"\'')
        return os.path.basename(fn)
    return ""


def probe(url: str, headers: dict | None = None, proxy: str | None = None):
    """返回 (size, support_range, filename)。size=0 表示未知大小。"""
    headers = headers or {}
    req = urllib.request.Request(url, headers={"User-Agent": UA, **headers}, method="HEAD")
    name = os.path.basename(urllib.parse.urlparse(url).path) or "download"
    try:
        with _open_compat(url, req, proxy=proxy) as r:
            size = int(r.headers.get("Content-Length") or 0)
            ranges = (r.headers.get("Accept-Ranges") or "").lower() == "bytes"
            cd_name = filename_from_headers(r.headers)
            if cd_name:
                name = cd_name
            if name.endswith(".php") and size:
                # 动态分发链接（res.php?id=xx），扩展名从 Content-Type 推断
                ct = (r.headers.get("Content-Type") or "").split(";")[0].strip()
                ext = {"application/zip": ".zip", "application/x-zip-compressed": ".zip",
                       "application/octet-stream": ".bin",
                       "application/vnd.android.package-archive": ".apk",
                       "application/pdf": ".pdf",
                       "application/x-msdownload": ".exe"}.get(ct)
                if ext:
                    name = os.path.splitext(name)[0] + ext
            return size, ranges, name
    except Exception:
        pass
    # HEAD 被拒则用 GET Range:0- 探测
    req = urllib.request.Request(url, headers={"User-Agent": UA, **headers})
    req.add_header("Range", "bytes=0-0")
    with _open_compat(url, req, proxy=proxy) as r:
        if r.status == 206:
            cr = r.headers.get("Content-Range", "")
            try:
                size = int(cr.split("/")[-1])
            except (ValueError, IndexError):
                size = 0
            return size, True, name
        size = int(r.headers.get("Content-Length") or 0)
        return size, False, name


# ============ work-stealing 块调度 ============
class BlockQueue:
    """共享块队列：初始大块 + 动态拆分。线程完成后偷别人的块尾巴。

    done_bytes / left_bytes 由内部锁保护；每块写入超过半程时把尾巴
    拆成新块放回队列，慢线程的剩余量被快线程自然分摊。
    """

    def __init__(self, size: int, blocks: list | None = None, parts: int = 16):
        """blocks: [[start,end,cursor],...] 精确快照；None 则全新切分。"""
        self.size = size
        self.lock = threading.Lock()
        self.blocks: list[list] = []
        if blocks is not None:
            for s, e, c in blocks:
                if c <= e:
                    self.blocks.append([s, e, c])
        else:
            for s, e in split_ranges(size, parts):
                self.blocks.append([s, e, s])
        self.left = sum(e - c + 1 for _, e, c in self.blocks)
        self.total_todo = self.left

    def snapshot(self) -> list:
        with self.lock:
            return [[b[0], b[1], b[2]] for b in self.blocks if b[2] <= b[1]]

    def steal(self, n: int = STEAL_CHUNK) -> tuple[int, int] | None:
        """领一块活。left 只在**落盘完成**时（commit）扣减，不再领取即扣。
        否则线程被 kill / giveback 未执行时 left 已归零，finished() 会把
        只写了首字节的稀疏文件误判为"已完成"（真事故：Studio 338MB 全零却报 100%）。
        返回 (start,end) 或 None。"""
        with self.lock:
            if not self.blocks:
                return None
            blk = max(self.blocks, key=lambda b: b[1] - b[2])
            remain = blk[1] - blk[2] + 1
            if remain <= 0:
                self.blocks.remove(blk)
                return None
            if remain <= n:
                self.blocks.remove(blk)
                return (blk[2], blk[1])
            # 拆尾巴：本线程拿 [end-n+1, end]，原块缩短
            s, e = blk[1] - n + 1, blk[1]
            blk[1] = s - 1
            if blk[1] < blk[2]:
                self.blocks.remove(blk)
            return (s, e)

    def giveback(self, start: int, end: int) -> None:
        """把没下完的块还回队列（线程失败/暂停退出时）。"""
        with self.lock:
            self.blocks.append([start, end, start])

    def commit(self, n: int) -> None:
        """n 字节已落盘：这里才是 left 的唯一扣减点。"""
        with self.lock:
            self.left -= n

    def finished(self) -> bool:
        with self.lock:
            return self.left <= 0


# ============ 限速器 ============
class RateLimiter:
    def __init__(self, bytes_per_sec: int):
        self.bps = bytes_per_sec
        self.event = threading.Event()
        self.event.set()

    def pause(self) -> None:
        self.event.clear()

    def resume(self) -> None:
        self.event.set()

    def wait(self, n: int) -> None:
        self.event.wait()
        if self.bps <= 0:
            return
        time.sleep(n / self.bps)


# ============ 下载任务 ============
class Task:
    """一个下载任务。pause()/resume()/stop() 线程安全。"""

    def __init__(self, url: str, dest: str, threads: int = 16,
                 headers: dict | None = None, proxy: str | None = None,
                 rate_limit: int = 0, retries: int = 5,
                 on_progress=None, on_done=None, on_error=None,
                 mirrors: list[str] | None = None,
                 auto_name: bool = False):
        self.url = url
        self.mirrors = [m for m in (mirrors or []) if m and m != url]
        self.dest = dest
        # auto_name: dest 的文件名是调用方从 URL 末段猜的（如 /shareDownload、
        # /res.php），真名在响应头 Content-Disposition 里。开启后以服务器
        # 声明的名为准，落盘路径在 _prepare() 里定稿。
        self.auto_name = auto_name
        self.threads = threads
        self.headers = headers or {}
        self.proxy = proxy
        self.rate = RateLimiter(rate_limit)
        self.retries = retries
        self.on_progress = on_progress    # fn(done_bytes, total_bytes, speed_bps)
        self.on_done = on_done            # fn()
        self.on_error = on_error          # fn(msg)

        self.size = 0
        self.q: BlockQueue | None = None
        self._fh = None
        self._file_lock = threading.Lock()
        self.prog_lock = threading.Lock()
        self.done_bytes = 0
        self.stop_flag = threading.Event()
        self.paused = threading.Event()
        self.workers: list[threading.Thread] = []
        self._local = threading.local()   # 每线程文件句柄（见 _get_file）
        self.error: str | None = None
        self.t0 = 0.0
        self.resume_base = 0
        self._last_save = 0.0
        self.state: dict = {}
        self._inflight: dict[int, list] = {}  # tid -> [start, end, cursor]
        self._inflight_lock = threading.Lock()

    # ---- 对外控制 ----
    def pause(self):
        self.paused.set()

    def resume(self):
        self.paused.clear()

    def stop(self):
        self.stop_flag.set()
        self.paused.clear()

    # ---- 速度统计 ----
    def _bump(self, n: int) -> None:
        """落盘记账的唯一入口：done_bytes（进度/速度）与 q.left（完成判定）
        同时推进，保证进度条 = 真实落盘量，与文件内容一致。"""
        if self.q:
            self.q.commit(n)
        with self.prog_lock:
            self.done_bytes += n

    def speed(self) -> float:
        with self.prog_lock:
            el = max(time.time() - self.t0, 1e-6)
            return max(self.done_bytes - getattr(self, "resume_base", 0), 0) / el

    def _tick(self) -> None:
        if self.on_progress:
            self.on_progress(self.done_bytes, self.size, self.speed())

    # ---- 主流程 ----
    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            self._download()
            if not self.stop_flag.is_set() and not self.error:
                if os.path.exists(state_path(self.dest)):
                    os.remove(state_path(self.dest))
                if self.on_done:
                    self.on_done()
        except Exception as e:
            self.error = str(e)
            if self.on_error:
                self.on_error(str(e))
        finally:
            self.close()

    def _prepare(self) -> bool:
        size, ranges, name = probe(self.url, self.headers, self.proxy)
        if not os.path.dirname(self.dest):
            self.dest = os.path.join(os.getcwd(), self.dest)
        # auto_name：文件名由服务器 Content-Disposition 决定（URL 末段是
        # shareDownload / res.php 这类占位符时，猜出来的名字必然错）。
        # 此时 dest 是"存放目录"，落盘名换成服务器声明的真名。
        if self.auto_name and name:
            d = self.dest
            if not (d.endswith(os.sep) or os.path.isdir(d)):
                d = os.path.dirname(d)
            self.dest = os.path.join(d, name)
        self.size = size
        # ---- 镜像池：同 size 且支持 Range 的镜像与主源混编 ----
        self._pool = [(self.url, True)]
        for m in getattr(self, "mirrors", []):
            try:
                msz, mr, _ = probe(m, self.headers, self.proxy)
                if msz == size and mr:
                    self._pool.append((m, True))
            except Exception:
                pass  # 不可用镜像直接剔除
        if size <= 0 or not ranges:
            return False  # 单线程流式
        blocks = None
        if not os.path.exists(self.dest) or os.path.getsize(self.dest) != size:
            with open(self.dest, "wb") as f:
                f.truncate(size)
        else:
            # 恢复：从状态文件读精确块快照 [start,end,cursor]
            try:
                with open(state_path(self.dest), "r", encoding="utf-8") as f:
                    st = json.load(f)
                if st.get("url") == self.url and st.get("size") == size and st.get("blocks"):
                    blocks = st["blocks"]
            except Exception:
                blocks = None
        self.q = BlockQueue(size, blocks, self.threads)
        self.state = {"url": self.url, "size": size}
        return True

    def _download(self) -> None:
        self.t0 = time.time()
        if not self._prepare():
            self._stream_single()
            return
        resume_base = self.size - self.q.left
        self.resume_base = resume_base
        self.done_bytes = resume_base

        for i in range(self.threads):
            t = threading.Thread(target=self._worker, args=(i,), daemon=True)
            t.start()
            self.workers.append(t)
        # 进度上报循环
        while any(w.is_alive() for w in self.workers):
            if self.stop_flag.is_set():
                return
            self._tick()
            self._save_if_needed()
            time.sleep(0.5)
        self._tick()
        if self.q and not self.q.finished() and not self.stop_flag.is_set():
            raise RuntimeError("分片未全部完成但线程已退出")
        self._save_if_needed(force=True)

    def speed_hint(self) -> float:
        return 10 * 1024 * 1024  # 续传时进度条起点的保守速度假设

    # ---- 代理 / 分块粒度 ----
    def _proxy_parts(self):
        """解析代理 URL（每任务一次，供各 worker 复用）。"""
        if not self.proxy:
            return None
        return urllib.parse.urlsplit(self.proxy)

    def _proxy_tunnel_headers(self) -> dict:
        """http://user:pass@host:port 形式的代理认证（CONNECT 阶段用）。"""
        p = self._proxy_parts()
        if p is None or not p.username:
            return {}
        raw = f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}"
        return {"Proxy-Authorization":
                "Basic " + base64.b64encode(raw.encode()).decode()}

    def _piece_size(self) -> int:
        """工作块大小：平时 8MB 省握手；尾部 < 4MB 切成 256KB 摊给多线程。"""
        left = self.q.left if self.q else 0
        if left <= 4 * 1024 * 1024:
            return TAIL_PIECE
        return STEAL_CHUNK

    def _save_if_needed(self, force: bool = False) -> None:
        if not self.q:
            return
        if force or time.time() - self._last_save > 1.0:
            self._last_save = time.time()
            self.state["blocks"] = self.full_snapshot()
            save_state(self.dest, self.state)

    def full_snapshot(self) -> list:
        """q 剩余块 + in-flight 块的已落盘前缀，供断点续传精确恢复。"""
        base = self.q.snapshot() if self.q else []
        with self._inflight_lock:
            inflight = [[s, e, c] for s, e, c in self._inflight.values()]
        # in-flight 块 [s,e,c]：c..e 还没落盘（cursor 语义同 q 块）
        return base + inflight

    # ---- 工作线程：keep-alive + work-stealing ----
    def _worker(self, idx: int) -> None:
        # 多源轮换：线程 idx 固定用 pool[idx % len(pool)]，天然负载均衡；
        # 单源任务 pool 只有主源，行为与原来完全一致
        pool = getattr(self, "_pool", None) or [(self.url, True)]
        url = pool[idx % len(pool)][0]
        parsed = urllib.parse.urlparse(url)
        https = parsed.scheme == "https"
        host = parsed.hostname
        port = parsed.port or (443 if https else 80)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        conn: http.client.HTTPConnection | None = None
        proxy_parts = self._proxy_parts()

        def connect():
            if proxy_parts is not None:
                # 真正的代理支持：HTTPS 目标走 CONNECT 隧道，HTTP 目标走绝对 URI。
                # 旧版这里只有 pass —— --proxy 只作用于 probe()，多线程传输全走直连，
                # 这就是"开了 VPN 挂 --proxy 速度没变"的根因。
                phost = proxy_parts.hostname
                pport = proxy_parts.port or 8080
                if https:
                    c = http.client.HTTPSConnection(
                        phost, pport, timeout=CONNECT_TIMEOUT, context=ssl_ctx)
                    c.set_tunnel(host, port, headers=self._proxy_tunnel_headers())
                    c.connect()
                    return c
                c = http.client.HTTPConnection(phost, pport, timeout=CONNECT_TIMEOUT)
                c.connect()
                return c
            if not https:
                c = http.client.HTTPConnection(host, port, timeout=CONNECT_TIMEOUT)
                c.connect()
                return c
            c = http.client.HTTPSConnection(host, port, timeout=CONNECT_TIMEOUT,
                                            context=ssl_ctx)
            try:
                c.connect()
                return c
            except ssl.SSLError as e:
                if not _is_legacy_ssl_error(e):
                    raise
                c = http.client.HTTPSConnection(host, port, timeout=CONNECT_TIMEOUT,
                                                context=ssl_ctx_legacy)
                c.connect()
                return c

        def target() -> str:
            """明文 HTTP 走代理时，请求行必须是绝对 URI。"""
            if proxy_parts is not None and not https:
                hostpart = host if port == 80 else f"{host}:{port}"
                return f"http://{hostpart}{path}"
            return path

        base_headers = {"User-Agent": UA, "Accept": "*/*", **self.headers}

        while not self.stop_flag.is_set():
            if self.paused.is_set():
                time.sleep(0.2)
                continue
            piece = self.q.steal(self._piece_size())
            if piece is None:
                break
            s, e = piece
            pos = s
            with self._inflight_lock:
                self._inflight[threading.get_ident()] = [s, e, s]
            for attempt in range(self.retries + 1):
                try:
                    if conn is None:
                        conn = connect()
                    req_headers = dict(base_headers)
                    req_headers["Range"] = f"bytes={pos}-{e}"
                    conn.request("GET", target(), headers=req_headers)
                    resp = conn.getresponse()
                    # 跟随重定向（http.client 不自动跟；下载场景要保 Range 续传）
                    if resp.status in (301, 302, 303, 307, 308):
                        loc = resp.getheader("Location") or ""
                        resp.read()
                        if not loc:
                            raise RuntimeError("重定向缺少 Location")
                        conn.close()
                        conn = None
                        new = urllib.parse.urlsplit(loc)
                        if new.scheme and new.netloc:
                            # 跨主机重定向：重定向本 worker 的目标
                            https = new.scheme == "https"
                            host = new.hostname or host
                            port = new.port or (443 if https else 80)
                            path = new.path + (f"?{new.query}" if new.query else "")
                        else:
                            path = loc if loc.startswith("/") else "/" + loc
                        if attempt < self.retries:
                            attempt -= 1  # 重定向不消耗重试次数
                        continue
                    if resp.status not in (200, 206):
                        raise RuntimeError(f"HTTP {resp.status}")
                    if resp.status == 200 and pos > s:
                        # 服务端忽略 Range，整文件重发：丢弃前面部分
                        skip = pos
                        while skip > 0:
                            buf = resp.read(min(skip, CHUNK))
                            if not buf:
                                break
                            skip -= len(buf)
                    while pos <= e:
                        if self.stop_flag.is_set():
                            return
                        if self.paused.is_set():
                            # 暂停：还块 + 关闭脏连接（半读的响应流不能复用）
                            self.q.giveback(pos, e)
                            with self._inflight_lock:
                                self._inflight.pop(threading.get_ident(), None)
                            try:
                                conn.close()
                            except Exception:
                                pass
                            conn = None
                            pos = e + 1  # 标记本块已交还
                            break
                        buf = resp.read(min(CHUNK, e - pos + 1))
                        if not buf:
                            break
                        # 无锁写：每线程独立句柄 + 块区间不重叠
                        f = self._get_file()
                        f.seek(pos)
                        f.write(buf)
                        pos += len(buf)
                        with self._inflight_lock:
                            self._inflight[threading.get_ident()][2] = pos
                        self._bump(len(buf))
                        self.rate.wait(len(buf))
                    if pos > e:
                        with self._inflight_lock:
                            self._inflight.pop(threading.get_ident(), None)
                        if resp.status == 200:
                            # 整文件响应但没读完就换块：连接不可复用
                            try:
                                conn.close()
                            except Exception:
                                pass
                            conn = None
                        break
                    if pos == e + 1:  # 暂停交还，回外层等唤醒
                        continue
                    # 提前断流：剩余量还回队列
                    raise ConnectionError("short read")
                except Exception:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
                        conn = None
                    if attempt >= self.retries or self.stop_flag.is_set():
                        # 还块：把未完成部分放回（pos==e+1 表示暂停时已还）
                        if s <= pos <= e:
                            self.q.giveback(pos, e)
                        if attempt >= self.retries or self.stop_flag.is_set():
                            with self._inflight_lock:
                                self._inflight.pop(threading.get_ident(), None)
                        if attempt >= self.retries:
                            raise
                    time.sleep(min(1.5 * (attempt + 1), 6))
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        self._close_thread_files()   # 回收本线程的文件句柄

    def _get_file(self):
        """每线程独占文件句柄。

        原实现全局单句柄 + _file_lock，把 N 线程写盘串行化成单线程，
        实测是比网络更硬的瓶颈。OS 允许同文件多句柄并发写（各自 seek），
        块间不重叠时无需加锁。close() 需在所有线程退出后调用。
        """
        fh = getattr(self._local, "fh", None)
        if fh is None:
            fh = open(self.dest, "r+b")
            self._local.fh = fh
        return fh

    def _close_thread_files(self) -> None:
        """worker 退出时关掉自己的句柄，避免句柄泄漏。"""
        fh = getattr(self._local, "fh", None)
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
            self._local.fh = None

    def close(self) -> None:
        with self._file_lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None

    # ---- 单线程流式（不支持 Range） ----
    def _stream_single(self) -> None:
        req = urllib.request.Request(self.url, headers={"User-Agent": UA, **self.headers})
        total = 0
        with _open_compat(self.url, req, timeout=60,
                          proxy=self.proxy) as r, open(self.dest, "wb") as f:
            cr = r.headers.get("Content-Length")
            self.size = int(cr) if cr else 0
            while True:
                if self.stop_flag.is_set():
                    return
                if self.paused.is_set():
                    time.sleep(0.2)
                    continue
                buf = r.read(CHUNK)
                if not buf:
                    break
                f.write(buf)
                total += len(buf)
                with self.prog_lock:
                    self.done_bytes = total
                self.rate.wait(len(buf))
                self._tick()
        self._tick()


# ============ 批量 CLI ============
def cli(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Super Flash Download 引擎 CLI")
    ap.add_argument("url")
    ap.add_argument("-o", "--out")
    ap.add_argument("-n", "--threads", type=int, default=16)
    ap.add_argument("-H", "--header", action="append", default=[])
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--limit", type=int, default=0, help="限速 B/s，0 不限")
    ap.add_argument("--mirror", action="append", default=[],
                    help="镜像源 URL，可多次")
    ap.add_argument("-r", "--retries", type=int, default=5)
    ap.add_argument("--auto-name", action="store_true",
                    help="文件名取服务器 Content-Disposition（URL 末段是占位符时用）")
    a = ap.parse_args(argv)
    headers = {k.strip(): v.strip() for h in a.header for k, v in [h.split(":", 1)]}

    dest = a.out or os.path.basename(urllib.parse.urlparse(a.url).path) or "download"
    if a.auto_name and a.out:
        os.makedirs(a.out, exist_ok=True)   # auto_name 时 out 是目录
    last = [0.0]

    def prog(done, total, spd):
        now = time.time()
        if now - last[0] > 0.4 or done == total:
            pct = f"{done * 100 / total:5.1f}%" if total else "  n/a"
            print(f"\r{pct} {done/1048576:8.1f}MB {spd/1048576:6.2f}MB/s", end="", flush=True)
            last[0] = now

    t = Task(a.url, dest, a.threads, headers, a.proxy, a.limit, a.retries,
             mirrors=a.mirror,
             auto_name=a.auto_name,
             on_progress=prog,
             on_done=lambda: print(f"\n[完成] {dest}"),
             on_error=lambda m: print(f"\n[失败] {m}", file=sys.stderr))
    t._run()  # CLI 模式同步跑（start() 是给 GUI 的异步入口）
    return 1 if t.error else 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))

"""代理链路与尾部小块的回归测试（离线，本地起上游 + 代理）。

为什么必须有这个测试：旧引擎 `_worker()` 里 `if self.proxy: pass`，
`--proxy` 只作用于 probe()，多线程传输全走直连 —— 表现就是"开了 VPN、
挂了 --proxy，速度一点没变"。单元测试必须能证明字节真的过了代理。
"""
import hashlib
import os
import socketserver
import sys
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from xyengine import (STEAL_CHUNK, TAIL_PIECE, BlockQueue, Task,  # noqa: E402
                      split_ranges)

DATA = bytes(bytearray((i * 37 + 11) % 256 for i in range(3 * 1024 * 1024)))


class Upstream(BaseHTTPRequestHandler):
    """支持 Range 的静态服务器（模拟 CDN）。"""

    hits = []

    def log_message(self, *a):
        pass

    def _serve(self, with_body: bool):
        Upstream.hits.append((self.command, self.headers.get("Range")))
        rng = self.headers.get("Range")
        body = DATA
        start = 0
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a or 0)
            end = int(b) if b else len(DATA) - 1
            body = DATA[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range",
                             f"bytes {start}-{start + len(body) - 1}/{len(DATA)}")
        else:
            self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if with_body:
            self.wfile.write(body)

    def do_GET(self):
        self._serve(True)

    def do_HEAD(self):
        self._serve(False)


class ProxyHandler(socketserver.StreamRequestHandler):
    """极简正向代理：把 target.invalid 解析成上游地址，模拟真实的代理 DNS 解析。"""

    seen = []
    upstream = ""      # fixture 里填 127.0.0.1:port

    def handle(self):
        line = self.rfile.readline().decode("latin-1").strip()
        if not line:
            return
        method, uri, _ = (line.split(" ") + ["", ""])[:3]
        headers = {}
        while True:
            h = self.rfile.readline().decode("latin-1").strip()
            if not h:
                break
            k, _, v = h.partition(":")
            headers[k.strip()] = v.strip()
        if method.upper() == "CONNECT":
            self.wfile.write(b"HTTP/1.1 502 CONNECT not supported here\r\n\r\n")
            return
        ProxyHandler.seen.append((method, uri))
        target = urllib.parse.urlsplit(uri)
        if not target.scheme:
            self.wfile.write(b"HTTP/1.1 400 absolute-uri required\r\n\r\n")
            return
        # 代理侧解析：target.invalid 只在代理里存在，直连必然 DNS 失败
        origin = uri.replace(f"{target.scheme}://{target.netloc}",
                             f"http://{ProxyHandler.upstream}", 1)
        req = urllib.request.Request(origin, method=method.upper())
        for k, v in headers.items():
            if k.lower() in ("host", "connection", "proxy-connection"):
                continue
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=20) as r:
            body = b"" if method.upper() == "HEAD" else r.read()
            out = [f"HTTP/1.1 {r.status} X", *[f"{k}: {v}" for k, v in r.headers.items()
                                               if k.lower() in ("content-length", "content-range",
                                                                "accept-ranges", "content-type")]]
            self.wfile.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))
            if body:
                self.wfile.write(body)


@pytest.fixture(scope="module")
def servers():
    up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    px = socketserver.ThreadingTCPServer(("127.0.0.1", 0), ProxyHandler)
    px.daemon_threads = True
    ProxyHandler.upstream = f"127.0.0.1:{up.server_address[1]}"
    threading.Thread(target=px.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{up.server_address[1]}/data.bin", \
          f"http://127.0.0.1:{px.server_address[1]}"
    up.shutdown()
    px.shutdown()


def test_download_goes_through_proxy(servers, tmp_path):
    """目标域名不可解析（target.invalid），只有走代理才可能成功。"""
    _, proxy = servers
    dest = str(tmp_path / "via_proxy.bin")
    ProxyHandler.seen.clear()
    t = Task("http://target.invalid/data.bin", dest, threads=8, proxy=proxy)
    t._run()
    assert t.error is None, t.error
    assert os.path.getsize(dest) == len(DATA)
    assert hashlib.sha256(open(dest, "rb").read()).hexdigest() == \
        hashlib.sha256(DATA).hexdigest()
    assert ProxyHandler.seen, "代理没收到任何请求：说明 worker 仍在直连"
    assert all(u.startswith("http://target.invalid") for _, u in ProxyHandler.seen)


def test_direct_download_still_works(servers, tmp_path):
    url, _ = servers
    dest = str(tmp_path / "direct.bin")
    t = Task(url, dest, threads=6)
    t._run()
    assert t.error is None, t.error
    assert hashlib.sha256(open(dest, "rb").read()).hexdigest() == \
        hashlib.sha256(DATA).hexdigest()


def test_tail_piece_shrinks_near_end():
    t = Task("http://x/y", "d", threads=4)
    t.q = BlockQueue(64 * 1024 * 1024, None, 4)
    assert t._piece_size() == STEAL_CHUNK
    t.q = BlockQueue(2 * 1024 * 1024, None, 4)
    assert t._piece_size() == TAIL_PIECE
    assert TAIL_PIECE < STEAL_CHUNK


def test_partition_covers_file_exactly():
    for size in (0, 1, 1000, 3 * 1024 * 1024, 64 * 1024 * 1024 + 7):
        rs = split_ranges(size, 16)
        if size == 0:
            continue
        assert rs[0][0] == 0 and rs[-1][1] == size - 1
        for (_, e1), (s2, _) in zip(rs, rs[1:]):
            assert s2 == e1 + 1

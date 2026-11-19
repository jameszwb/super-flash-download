"""Super Flash Download引擎 IPv4 优先连接层 + 连接器自启动 的回归测试。

跑法（venv 内，pytest 已装）：
    python -m pytest tests/test_ipv4_first.py -q
"""
from __future__ import annotations

import os
import socket
import sys

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import xyengine as eng  # noqa: E402


def test_prefer_ipv4_puts_v4_first():
    """IPv4 条目必须排到 IPv6 前面（稳定排序，族内原序不变）。"""
    v6a = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606::1", 443, 0, 0))
    v6b = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606::2", 443, 0, 0))
    v4a = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.18.0.79", 443))
    v4b = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.18.1.79", 443))
    out = eng._prefer_ipv4([v6a, v6b, v4a, v4b])
    assert [ai[0] for ai in out] == [socket.AF_INET, socket.AF_INET,
                                    socket.AF_INET6, socket.AF_INET6]
    # 族内原序保持
    assert out[0][4] == v4a[4]
    assert out[1][4] == v4b[4]


def test_prefer_ipv4_empty_and_single():
    assert eng._prefer_ipv4([]) == []
    only6 = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0))]
    assert eng._prefer_ipv4(only6) == only6


def test_getaddrinfo_patched_to_ipv4_first():
    """模块导入后 socket.getaddrinfo 必须已被包成 IPv4 优先版。"""
    assert socket.getaddrinfo is eng._getaddrinfo_ipv4_first
    # 真实解析：结果里 IPv4 应排在 IPv6 前面（若该域名有 AAAA）
    infos = socket.getaddrinfo("hermes-assets.nousresearch.com", 443,
                                socket.AF_UNSPEC, socket.SOCK_STREAM)
    fams = [ai[0] for ai in infos]
    if socket.AF_INET in fams and socket.AF_INET6 in fams:
        assert fams.index(socket.AF_INET) < fams.index(socket.AF_INET6)


def test_tls_handshake_via_ipv4_succeeds_quickly():
    """回归防线：本机 IPv6 到 Cloudflare 有 PMTU 黑洞，
    走 IPv6 握手会 20~30s 超时；IPv4 应该在几秒内完成握手。"""
    import ssl
    import time

    host = "hermes-assets.nousresearch.com"
    t0 = time.time()
    ctx = ssl.create_default_context()
    raw = socket.create_connection((host, 443), timeout=10)
    s = ctx.wrap_socket(raw, server_hostname=host)
    elapsed = time.time() - t0
    try:
        assert s.version() in ("TLSv1.2", "TLSv1.3")
        assert elapsed < 10, f"TLS 握手耗时 {elapsed:.1f}s，疑似又走回 IPv6 黑洞"
    finally:
        s.close()


@pytest.mark.parametrize("probe_url", [
    "https://hermes-assets.nousresearch.com/Hermes-Setup.exe?build=f4af5c339142",
])
def test_probe_returns_size(probe_url):
    """probe() 必须拿到真实大小 + Range 支持（探测层修好后不再抛超时）。"""
    size, ranges, name = eng.probe(probe_url)
    assert size > 0
    assert ranges is True
    assert name.endswith(".exe")

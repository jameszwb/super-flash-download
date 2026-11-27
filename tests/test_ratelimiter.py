"""限速器的行为测试：额度必须是「整个任务的总额」，不是「每条连接各一份」。

修复前的实现是每个 worker 各睡 n/bps，4 条连接就会跑出 4 倍速度；
这些用例会红。断言的是真实耗时，不是源码里有没有某个字符串。
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xyengine import RateLimiter  # noqa: E402


def _run(limiter: RateLimiter, threads: int, chunks: int, chunk: int) -> None:
    def worker():
        for _ in range(chunks):
            limiter.wait(chunk)

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


def test_total_rate_is_shared_across_threads():
    """4 线程 × 5 块 × 20KB，限额 200KB/s → 总耗时约 2.0 秒。
    每连接单独限额的实现只要约 0.5 秒（快 4 倍），必须判红。"""
    bps, chunk, threads, chunks = 200_000, 20_000, 4, 5
    limiter = RateLimiter(bps)
    expect = threads * chunks * chunk / bps      # 2.0s
    t0 = time.monotonic()
    _run(limiter, threads, chunks, chunk)
    elapsed = time.monotonic() - t0
    assert elapsed >= expect * 0.85, (
        f"总额度被突破：{elapsed:.2f}s < {expect:.2f}s（多线程各自限速的症状）")
    assert elapsed <= expect * 1.8, f"限速过度：{elapsed:.2f}s 远超 {expect:.2f}s"


def test_unlimited_returns_immediately():
    limiter = RateLimiter(0)
    t0 = time.monotonic()
    _run(limiter, 8, 20, 1 << 20)
    assert time.monotonic() - t0 < 1.0


def test_pause_blocks_and_resume_continues():
    """暂停期间不得推进额度：0.6s 暂停应体现在耗时里。"""
    limiter = RateLimiter(1_000_000)
    limiter.pause()
    holder = {}

    def worker():
        limiter.wait(100_000)

    th = threading.Thread(target=worker)
    t0 = time.monotonic()
    th.start()
    time.sleep(0.6)
    holder["blocked_for"] = time.monotonic() - t0
    limiter.resume()
    th.join(timeout=5)
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.6, f"暂停没有生效（{elapsed:.2f}s）"
    assert not th.is_alive(), "恢复后没有继续"
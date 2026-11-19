"""引擎端到端测试：真下载 / md5 对照 / work-stealing 分片完整性 / 暂停恢复 / 续传。"""
import hashlib
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from xyengine import (BlockQueue, Task, load_state, probe,  # noqa: E402
                      save_state, split_ranges, state_path)

URL = "https://nodejs.org/dist/v20.11.1/node-v20.11.1-win-x64.zip"
DEST = "engine_test.bin"
RESULT = {}


def sha(b):
    return hashlib.sha256(b).hexdigest()


def official_sha():
    last = None
    for i in range(5):
        try:
            txt = urllib.request.urlopen(
                "https://nodejs.org/dist/v20.11.1/SHASUMS256.txt", timeout=60).read().decode()
            return [l.split()[0] for l in txt.splitlines()
                    if l.endswith("node-v20.11.1-win-x64.zip")][0]
        except Exception as e:
            last = e
            time.sleep(1 + i)
    raise last


# ---- 1. BlockQueue 单测 ----
def test_blockqueue():
    q = BlockQueue(10_000_000, None, 8)
    got = []
    while True:
        p = q.steal(500_000)
        if p is None:
            break
        got.append(p)
    cov = sum(e - s + 1 for s, e in got)
    assert cov == 10_000_000, f"覆盖 {cov} != 10000000"
    # 无重叠
    got.sort()
    for a, b in zip(got, got[1:]):
        assert b[0] > a[1], f"重叠 {a} {b}"
    # snapshot -> 重建 -> 再偷 = 空
    q2 = BlockQueue(10_000_000, q.snapshot(), 8)
    assert q2.steal() is None, "快照重建后仍有剩余"
    print("[1] BlockQueue 覆盖/无重叠/快照往返 OK")


# ---- 2. 真下载 + sha256 对照 ----
def test_download():
    official = official_sha()
    for p in (DEST, state_path(DEST)):
        if os.path.exists(p):
            os.remove(p)
    done_ev = {}
    t = Task(URL, DEST, threads=16,
             on_done=lambda: done_ev.update(ok=True),
             on_error=lambda m: done_ev.update(err=m))
    t._run()  # 同步
    assert done_ev.get("ok"), f"失败: {done_ev.get('err')}"
    data = open(DEST, "rb").read()
    got = sha(data)
    print(f"[2] 真下载 {len(data)/1048576:.1f}MB sha 一致={got == official} "
          f"均速={t.speed()/1048576:.1f}MB/s(含统计尾巴)")
    assert got == official
    RESULT["official"] = official


# ---- 3. 暂停/恢复（限速场景下可观测） ----
def test_pause_resume():
    for p in ("pr.bin", state_path("pr.bin")):
        if os.path.exists(p):
            os.remove(p)
    t = Task("https://nodejs.org/dist/v20.11.1/node-v20.11.1-win-x64.zip",
             "pr.bin", threads=8, rate_limit=2_000_000)
    t.start()
    time.sleep(3)
    t.pause()
    # pause 只停「新领取的块」，已在途的连接仍会把手里那块写完落盘
    # （物理在途窗口 = 线程数 x CHUNK）。必须等窗口排空（done_bytes 收敛）
    # 才能断言「不再增长」——固定 sleep 1.5s 会在排空未完成时误判为红灯。
    window = 8 * 256 * 1024
    deadline = time.time() + 15
    prev = t.done_bytes
    stable = 0
    while time.time() < deadline:
        time.sleep(0.5)
        cur = t.done_bytes
        stable = stable + 1 if cur == prev else 0
        prev = cur
        if stable >= 3:      # 连续 1.5s 无增长 = 窗口已排空
            break
    d1 = t.done_bytes
    time.sleep(2)
    d2 = t.done_bytes
    t.resume()
    marks = []
    for _ in range(10):
        time.sleep(0.5)
        marks.append(t.done_bytes)
    d3 = t.done_bytes
    print("    resume 后心跳:", [m - d2 for m in marks])
    t.stop()
    time.sleep(1)
    print(f"[3] 排空后暂停净增={d2-d1}B(应=0) 恢复后增量={d3-d2}B(应>1.5MB)")
    assert d2 - d1 == 0, "暂停后仍在持续下载"
    assert d3 - d2 > 1_500_000, "恢复后没继续下"
    assert os.path.exists(state_path("pr.bin")), "stop 后应有状态文件供续传"


# ---- 4. 续传：stop 后重跑，sha 一致 ----
def test_resume():
    official = official_sha()
    t = Task(URL, "pr.bin", threads=8)
    t._run()
    data = open("pr.bin", "rb").read()
    got = sha(data)
    st_exists = os.path.exists(state_path("pr.bin"))
    print(f"[4] 续传后 sha 一致={got == official} 状态文件已清理={not st_exists}")
    assert got == official
    assert not st_exists


# ---- 5. probe ----
def test_probe():
    size, ranges, name = probe(URL)
    print(f"[5] probe size={size} range={ranges} name={name}")
    assert size == 29705015 and ranges and name.endswith(".zip")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    test_blockqueue()
    test_probe()
    t0 = time.time()
    test_download()
    dt1 = time.time() - t0
    test_pause_resume()
    t0 = time.time()
    test_resume()
    dt2 = time.time() - t0
    print(f"\nALL PASS  首下 {dt1:.1f}s  续传补完 {dt2:.1f}s")

"""BT 端到端冒烟脚本 —— 手动运行，不是 pytest 用例（`pytest tests/` 不会收集它）。

运行：python examples/bt_smoke.py
需要 Python 3.13 + libtorrent，会真实联网下载 Sintel 片段（约 15MB）。

"""
"""BT 终验：完整磁力链任务走 xybt.BTTask，验证 元数据→找节点→下载→完成 全链路。
挑小体积 + 多做种的种子（约 15MB 的 Sintel 预告片片段包，Blender 官方开放版权）。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import xybt  # noqa: E402

# Sintel 官方种子（archive.org 做种，版权开放）
MAGNET = ("magnet:?xt=urn:btih:08ada5a7a6183aae1e09d831df6748d566095a10"
          "&dn=Sintel&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce"
          "&tr=https%3A%2F%2Fbt.archive.org%2Fannounce")

SAVE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "bt_test_dl")
os.makedirs(SAVE, exist_ok=True)

t = xybt.BTTask(MAGNET, SAVE, port=16885,
                on_status=lambda s: print("  [status]", s, flush=True),
                on_progress=lambda d, tot, sp: print(
                    f"  [prog] {d/1048576:.1f}/{tot/1048576:.1f}MB {sp}", flush=True),
                on_done=lambda: print("DONE", flush=True),
                on_error=lambda m: print("ERR", m, flush=True))
t.start()
print("任务启动:", t.name, flush=True)

deadline = time.time() + 360
done_flag = False
while time.time() < deadline:
    time.sleep(3)
    if t.error:
        sys.exit(1)
    if t.handle and t.handle.status().is_finished:
        done_flag = True
        break
    if t.handle and t.handle.status().total_done >= t.handle.status().total_wanted > 0:
        done_flag = True
        break

print("FINISHED:", done_flag)
# 落盘清单
for root, _, fs in os.walk(SAVE):
    for f in fs:
        p = os.path.join(root, f)
        print(f"  {os.path.getsize(p):>12,}  {os.path.relpath(p, SAVE)}")
sys.exit(0 if done_flag else 1)

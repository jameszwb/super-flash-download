#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Super Flash Download BT 后端 —— libtorrent 封装（磁力链 / .torrent），接口对齐 xyengine.Task

pause()/resume()/stop()/on_progress/on_done/on_error 与 Task 同构，
GUI 无感复用。需 venv（Python 3.13）内运行：pip install libtorrent
"""
from __future__ import annotations

import os
import threading
import time

try:
    import libtorrent as lt
    HAS_LT = True
except ImportError:
    HAS_LT = False


def _settings(save_path: str, port: int) -> dict:
    return {
        "listen_interfaces": f"0.0.0.0:{port}",
        "alert_mask": lt.alert.category_t.error_notification,
        "active_downloads": 8,
        "active_seeds": 8,
        "active_limit": 16,
        "download_rate_limit": 0,      # 0=不限
        "upload_rate_limit": 0,
        "enable_dht": True,
        "dht_bootstrap_nodes": "router.bittorrent.com:6881,dht.transmissionbt.com:6881,router.utorrent.com:6881,dht.aelitis.com:6881",
        "enable_lsd": True,
        "enable_upnp": True,
        "enable_natpmp": True,
    }


class BTTask:
    """一个 BT/磁力下载任务。start() 异步，控制接口与 xyengine.Task 一致。"""

    def __init__(self, source: str, save_path: str, port: int = 16881,
                 rate_limit: int = 0, on_progress=None, on_done=None,
                 on_error=None, on_status=None):
        if not HAS_LT:
            raise RuntimeError("libtorrent 未安装：请用 Python 3.13 环境并 pip install libtorrent")
        self.source = source          # magnet:?... 或 .torrent 文件路径
        self.save_path = save_path
        self.port = port
        self.rate_limit = rate_limit
        self.on_progress = on_progress  # fn(done_b, total_b, speed_str)
        self.on_done = on_done
        self.on_error = on_error
        self.on_status = on_status      # fn(text)  如"找节点中 peers=3"

        self.handle = None
        self.session = None
        self.name = os.path.basename(source)  # .torrent 兜底；磁力会覆盖
        self.stop_flag = threading.Event()
        self.paused_flag = threading.Event()
        self.error: str | None = None
        self._peak = 1.0
        self._speed_smooth = 0.0
        self._t = threading.Thread(target=self._run, daemon=True)

    # ---- 控制（对齐 Task） ----
    def start(self):
        self._t.start()

    def pause(self):
        self.paused_flag.set()
        try:
            if self.handle:
                self.handle.pause()
        except Exception:
            pass

    def resume(self):
        self.paused_flag.clear()
        try:
            if self.handle:
                self.handle.resume()
        except Exception:
            pass

    def stop(self):
        self.stop_flag.set()
        self.paused_flag.clear()
        try:
            if self.handle:
                self.handle.pause()
        except Exception:
            pass

    # ---- 主流程 ----
    def _run(self):
        try:
            self.session = lt.session(_settings(self.save_path, self.port))
            # 2.x 的 dict 构造对 dht_bootstrap_nodes 不生效，起会话后补一发
            self.session.apply_settings({
                "enable_dht": True,
                "dht_bootstrap_nodes": "router.bittorrent.com:6881,"
                                       "dht.transmissionbt.com:6881,"
                                       "router.utorrent.com:6881",
            })
            atp = lt.add_torrent_params()
            if self.source.startswith("magnet:"):
                atp = lt.parse_magnet_uri(self.source)
                self.name = atp.name or self.name
            else:
                if not os.path.isfile(self.source):
                    raise FileNotFoundError(self.source)
                atp = lt.torrent_info(self.source)
                self.name = atp.name()
            atp.save_path = self.save_path
            self.handle = self.session.add_torrent(atp)

            if self.rate_limit:
                self.session.apply_settings(
                    {"download_rate_limit": self.rate_limit})

            # 元数据等待（磁力链需要 DHT 找节点）
            t0 = time.time()
            while not self.handle.status().has_metadata:
                if self.stop_flag.is_set():
                    return
                st = self.handle.status()
                if self.on_status:
                    self.on_status(f"找节点中 peers={st.num_peers} {int(time.time()-t0)}s")
                if time.time() - t0 > 180:
                    raise TimeoutError("180 秒未找到节点（资源太冷或网络受限）")
                time.sleep(1)
            if self.on_status:
                self.on_status("BT 下载中")

            info = self.handle.torrent_file()
            self.name = info.name()
            total = info.total_size()
            last_done = 0
            while True:
                if self.stop_flag.is_set():
                    return
                st = self.handle.status()
                done = int(st.total_done)
                bps = float(st.download_payload_rate)
                self._speed_smooth = bps if self._speed_smooth == 0 else \
                    self._speed_smooth * 0.7 + bps * 0.3
                if self.on_progress:
                    self.on_progress(done, total, self._fmt(self._speed_smooth))
                wanted = int(st.total_wanted) or total
                if done >= wanted and wanted > 0:
                    break
                last_done = done
                time.sleep(0.5)
            if self.on_progress:
                self.on_progress(total, total, self._fmt(0))
            if self.on_done:
                self.on_done()
        except Exception as e:
            self.error = str(e)
            if self.on_error:
                self.on_error(str(e))
        finally:
            try:
                if self.session:
                    self.session.pause()
            except Exception:
                pass

    def _fmt(self, bps: float) -> str:
        if bps >= 1048576:
            return f"{bps / 1048576:.1f} MB/s"
        if bps >= 1024:
            return f"{bps / 1024:.0f} KB/s"
        return f"{bps:.0f} B/s"

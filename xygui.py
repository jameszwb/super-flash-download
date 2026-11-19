#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Super Flash Download 3.0 —— 多线程下载器 GUI（PySide6）

布局参考迅雷：左侧分类导航（全部/下载中/已完成/失败 + 计数）+ 顶部搜索
+ 新建对话框 + 卡片式任务行（文件类型徽标/两行信息/细进度条/行内操作）。

修复 2.x 两个真 bug：
1. 僵尸"下载中"记录：_load_history 原样恢复上次强退时的 running 状态
   → 启动时状态归一化（running/paused -> interrupted，可续传可删）
2. 删除错位：QTableWidget.removeRow 后 rows 映射不更新，第二次删除定位错行
   -> 换 QListWidget，每行独立 widget，行身份永不变化
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs

os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")

from PySide6.QtCore import QPointF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QIcon, QLinearGradient, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFileDialog,
                               QFormLayout, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QProgressBar,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)

import xyengine as eng
try:
    import xybt
    HAS_BT = xybt.HAS_LT
except ImportError:
    HAS_BT = False

# PyInstaller onefile: __file__ points into a temp dir that is deleted on exit.
# RES_DIR holds bundled assets (_MEIPASS); APP_DIR holds user data (next to the exe).
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RES_DIR = getattr(sys, "_MEIPASS", APP_DIR)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    RES_DIR = APP_DIR

HISTORY_PATH = os.path.join(APP_DIR, "history.json")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
API_PORT = 16901

ACCENT = "#2f81f7"
GOOD = "#3fb950"
WARN = "#d29922"
BAD = "#f85149"
TXT = "#e6edf3"
SUB = "#8b949e"

STYLE = """
QWidget { background: #0d1117; color: #e6edf3; font-size: 13px; }
#root { background: #0d1117; }
#sidebar { background: #010409; border-right: 1px solid #21262d; }
#brand { font-size: 17px; font-weight: 800; color: #e6edf3; background: transparent; }
#brandSub { color: #8b949e; font-size: 10px; background: transparent; }
QListWidget#nav { background: transparent; border: none; outline: none; padding: 4px 0; }
QListWidget#nav::item { color: #8b949e; border-radius: 8px; padding: 9px 12px; margin: 2px 0; }
QListWidget#nav::item:hover { background: #161b22; color: #e6edf3; }
QListWidget#nav::item:selected { background: #1f6feb22; color: #58a6ff; font-weight: 700; }
QLineEdit {
    background: #010409; border: 1px solid #30363d; border-radius: 8px;
    padding: 8px 12px; selection-background-color: #1f6feb;
}
QLineEdit:focus { border: 1px solid #58a6ff; }
QPushButton {
    background: #21262d; border: 1px solid #30363d; border-radius: 8px;
    padding: 8px 16px; font-weight: 600;
}
QPushButton:hover { background: #30363d; border-color: #8b949e; }
#primary { background: #1f6feb; border: none; color: white; }
#primary:hover { background: #388bfd; }
#ghost { background: transparent; border: 1px solid #30363d; color: #8b949e; }
QListWidget#tasks { background: #0d1117; border: none; outline: none; }
QListWidget#tasks::item { border-bottom: 1px solid #010409; }
QListWidget#tasks::item:selected { background: #161b22; }
QListWidget#tasks::item:hover { background: #10161f; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #30363d; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #484f58; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
#card { background: #161b22; border: 1px solid #30363d; border-radius: 12px; }
#statVal { font-size: 19px; font-weight: 800; color: #58a6ff; background: transparent; }
#statLabel { color: #8b949e; font-size: 11px; background: transparent; }
#rowName { font-size: 13.5px; font-weight: 700; color: #e6edf3; background: transparent; }
#rowSub { font-size: 11.5px; color: #8b949e; background: transparent; }
#rowPct { font-size: 15px; font-weight: 800; color: #58a6ff; background: transparent; }
#iconBadge { border-radius: 9px; color: white; font-size: 10px; font-weight: 800;
             background: #1f6feb; }
QMenu { background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 6px; }
QMenu::item { padding: 8px 26px; border-radius: 6px; }
QMenu::item:selected { background: #1f6feb; color: white; }
#empty { color: #484f58; font-size: 14px; background: transparent; }
#headTitle { font-size: 16px; font-weight: 800; background: transparent; }
"""


def fmt_speed(bps: float) -> str:
    if bps >= 1048576:
        return f"{bps / 1048576:.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps:.0f} B/s"


def fmt_size(b: float) -> str:
    if b >= 1073741824:
        return f"{b / 1073741824:.2f} GB"
    if b >= 1048576:
        return f"{b / 1048576:.1f} MB"
    if b >= 1024:
        return f"{b / 1024:.0f} KB"
    return f"{b:.0f} B"


def fmt_eta(sec: float) -> str:
    if sec <= 0 or sec > 86400 * 365:
        return "--"
    m, s = divmod(int(sec), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}m"
    return f"{m:02d}:{s:02d}"


VIDEO = "MP4 MOV MKV AVI TS WEBM FLV".split()
AUDIO = "MP3 FLAC WAV M4A AAC OGG".split()
ARCHIVE = "ZIP RAR 7Z TAR GZ XZ ISO".split()
EXEC = "EXE MSI APK DMG DEB".split()
IMAGE = "JPG PNG GIF WEBP BMP SVG ICO".split()
MODEL = "GGUF SAFETENSORS ONNX BIN PT".split()


def ext_badge(name: str) -> tuple:
    """(徽标文字, 颜色) 按扩展名分色。"""
    ext = os.path.splitext(name)[1].lstrip(".").upper()[:4] or "FILE"
    if ext in VIDEO:
        return ext, "#8957e5"
    if ext in AUDIO:
        return ext, "#bf3989"
    if ext in ARCHIVE:
        return ext, "#d29922"
    if ext in EXEC:
        return ext, "#f85149"
    if ext in IMAGE:
        return ext, "#3fb950"
    if ext in MODEL:
        return ext, "#58a6ff"
    return ext, "#4a5b75"


STATE_META = {
    "running": (ACCENT, "下载中"),
    "paused": (WARN, "已暂停"),
    "done": (GOOD, "已完成"),
    "error": (BAD, "失败"),
    "interrupted": (WARN, "已中断"),
}


class SpeedSpark(QWidget):
    """迷你速度面积图。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.data = [0.0] * 120
        self.peak = 1.0
        self.setFixedHeight(52)

    def push(self, bps: float) -> None:
        self.data.pop(0)
        self.data.append(bps)
        self.peak = max(max(self.data), 1.0)
        self.update()

    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        n = len(self.data)
        pts = [(i * w / (n - 1), h - (v / self.peak) * (h - 6) - 3)
               for i, v in enumerate(self.data)]
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0, QColor(47, 129, 247, 70))
        grad.setColorAt(1, QColor(47, 129, 247, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawPolygon(QPolygonF([QPointF(0, h)] + [QPointF(x, y) for x, y in pts]
                                + [QPointF(w, h)]))
        p.setPen(QPen(QColor(47, 129, 247), 1.6))
        p.setBrush(Qt.NoBrush)
        for i in range(1, len(pts)):
            p.drawLine(QPointF(*pts[i - 1]), QPointF(*pts[i]))


class TaskWorker(QThread):
    """HTTP 任务线程。"""

    progress = Signal(int, float, float, str)
    status = Signal(int, str)

    def __init__(self, tid, url, dest, threads, headers, proxy, limit,
                 mirrors=None, auto_name=False):
        super().__init__()
        self.tid = tid
        self.url = url
        self.dest = dest
        self.threads = threads
        self.headers = headers
        self.proxy = proxy
        self.limit = limit
        self.mirrors = mirrors or []
        self.auto_name = auto_name
        self.task = None
        self._last = 0.0
        self._last_b = 0
        self._speed = 0.0

    def run(self) -> None:
        def prog(done, total, _spd):
            now = time.time()
            dt = now - self._last
            if dt >= 0.4:
                inst = (done - self._last_b) / dt
                self._speed = inst if self._speed == 0 else self._speed * 0.7 + inst * 0.3
                self._last, self._last_b = now, done
            self.progress.emit(self.tid, done, total, fmt_speed(self._speed))

        self.task = eng.Task(
            self.url, self.dest, threads=self.threads, headers=self.headers,
            proxy=self.proxy, rate_limit=self.limit, mirrors=self.mirrors,
            auto_name=self.auto_name,
            on_progress=prog,
            on_done=lambda: self.status.emit(self.tid, "done"),
            on_error=lambda m: self.status.emit(self.tid, m))
        self.status.emit(self.tid, "running")
        self.task._run()

    def pause(self):
        if self.task:
            self.task.pause()

    def resume(self):
        if self.task:
            self.task.resume()

    def stop(self):
        if self.task:
            self.task.stop()


class TaskRow(QWidget):
    """卡片式任务行：徽标 | 名称+副行+进度条 | 百分比 | 操作。"""

    def __init__(self, tid: int, name: str, parent=None):
        super().__init__(parent)
        self.tid = tid
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 10, 12, 10)
        h.setSpacing(12)

        ext, color = ext_badge(name)
        badge = QLabel(ext or "FILE", objectName="iconBadge")
        badge.setFixedSize(40, 40)
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(f"#iconBadge {{ background: {color}; }}")
        h.addWidget(badge)

        mid = QVBoxLayout()
        mid.setSpacing(4)
        self.name_lbl = QLabel(name, objectName="rowName")
        self.name_lbl.setWordWrap(False)
        self.sub_lbl = QLabel("", objectName="rowSub")
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(5)
        self.bar.setStyleSheet(
            "QProgressBar { background:#21262d; border-radius:2px; }"
            "QProgressBar::chunk { border-radius:2px; background:#2f81f7; }")
        mid.addWidget(self.name_lbl)
        mid.addWidget(self.sub_lbl)
        mid.addWidget(self.bar)
        h.addLayout(mid, 1)

        self.pct_lbl = QLabel("0%", objectName="rowPct")
        self.pct_lbl.setFixedWidth(60)
        self.pct_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        h.addWidget(self.pct_lbl)

        ops = QHBoxLayout()
        ops.setSpacing(6)
        self.btn_pause = QPushButton("\u23f8")
        self.btn_open = QPushButton("\U0001F4C2")
        self.btn_del = QPushButton("\u2715")
        for b in (self.btn_pause, self.btn_open):
            b.setFixedSize(30, 26)
            b.setStyleSheet("QPushButton { padding: 0; font-size: 13px; }")
        self.btn_del.setFixedSize(30, 26)
        self.btn_del.setStyleSheet(
            "QPushButton { padding: 0; color: #f85149; font-size: 13px; }")
        self.btn_del.setToolTip("删除记录")
        self.btn_pause.setToolTip("暂停 / 继续")
        self.btn_open.setToolTip("打开目录")
        self.btn_pause.clicked.connect(lambda: self.window().toggle_pause(self.tid))
        self.btn_open.clicked.connect(lambda: self.window().open_dir(self.tid))
        self.btn_del.clicked.connect(lambda: self.window().delete_task(self.tid))
        ops.addWidget(self.btn_pause)
        ops.addWidget(self.btn_open)
        ops.addWidget(self.btn_del)
        h.addLayout(ops)

        self._size_txt = ""
        self._pct = 0

    def set_name(self, name: str) -> None:
        """更新任务显示名（auto_name 时真名由服务器 Content-Disposition 决定）。"""
        self.name_lbl.setText(name)
        ext, color = ext_badge(name)
        badge = self.findChild(QLabel, "iconBadge")
        if badge:
            badge.setText(ext or "FILE")

    def set_state(self, state: str, note: str = "", bt_label: str = "") -> None:
        color, label = STATE_META.get(state, (SUB, state))
        if bt_label and state == "running":
            label = bt_label
        extra = f" · {note}" if note else ""
        self.sub_lbl.setText(
            f'<span style="color:{color};font-weight:700">{label}</span>'
            f'<span style="color:{SUB}">{extra}</span>'
            f'<span style="color:{SUB}"> · {self._size_txt}</span>')
        done = state == "done"
        alive = state in ("running", "paused", "interrupted")
        self.btn_pause.setVisible(alive)
        self.btn_pause.setText("\u25b6" if state in ("paused", "interrupted") else "\u23f8")
        self.bar.setVisible(not done)
        self.pct_lbl.setText("\u2713" if done else f"{self._pct}%")
        self.pct_lbl.setStyleSheet(
            f"#rowPct {{ color: {GOOD if done else ACCENT}; }}")

    def set_progress(self, done: float, total: float, speed: str, eta: str) -> None:
        if total:
            self._pct = min(int(done * 100 / total), 100)
            self.bar.setValue(self._pct)
            self._size_txt = f"{fmt_size(done)} / {fmt_size(total)}"
        else:
            self._size_txt = fmt_size(done)
        self.pct_lbl.setText(f"{self._pct}%")
        speed_txt = f" · {speed}" if speed and speed != "0 B/s" else ""
        eta_txt = f" · 剩余{eta}" if eta and eta != "--" else ""
        color, _ = STATE_META["running"]
        self.sub_lbl.setText(
            f'<span style="color:{color};font-weight:700">下载中</span>'
            f'<span style="color:{SUB}"> · {self._size_txt}{speed_txt}{eta_txt}</span>')


class NewTaskDialog(QDialog):
    """新建下载对话框：URL / 目录 / 线程 / 限速。"""

    def __init__(self, parent, last_dir: str, has_bt: bool):
        super().__init__(parent)
        self.setWindowTitle("新建下载")
        self.setFixedWidth(580)
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 16)
        v.setSpacing(12)
        form = QFormLayout()
        form.setSpacing(10)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("http(s) 直链 / magnet:?... 磁力链 / .torrent 文件路径")
        self.dir_edit = QLineEdit(last_dir)
        btn_dir = QPushButton("…", objectName="ghost")
        btn_dir.setFixedWidth(34)
        btn_dir.clicked.connect(self._pick)
        drow = QHBoxLayout()
        drow.addWidget(self.dir_edit, 1)
        drow.addWidget(btn_dir)
        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 64)
        self.spin_threads.setValue(16)
        self.combo_limit = QComboBox()
        for label, val in [("不限速", 0), ("512 KB/s", 524288), ("1 MB/s", 1048576),
                           ("2 MB/s", 2097152), ("5 MB/s", 5242880)]:
            self.combo_limit.addItem(label, val)
        self.mirrors_edit = QLineEdit()
        self.mirrors_edit.setPlaceholderText("可选·每行一个镜像 URL（同一文件的其它源，多源并发加速）")
        self.mirrors_edit.setFixedHeight(56)
        form.addRow("链接", self.url_edit)
        form.addRow("保存到", drow)
        form.addRow("线程数", self.spin_threads)
        form.addRow("限速", self.combo_limit)
        form.addRow("镜像加速", self.mirrors_edit)
        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText("可选·如 http://127.0.0.1:7890（留空＝跟随系统代理）")
        form.addRow("代理", self.proxy_edit)
        v.addLayout(form)
        if not has_bt:
            tip = QLabel("⚠ 当前解释器未装 libtorrent，磁力/种子不可用（桌面快捷方式已指向 venv）")
            tip.setStyleSheet(f"color:{WARN}; background:transparent; font-size:11px;")
            tip.setWordWrap(True)
            v.addWidget(tip)
        btns = QHBoxLayout()
        btns.addStretch()
        btnCancel = QPushButton("取消")
        btnCancel.clicked.connect(self.reject)
        btnOk = QPushButton("开始下载", objectName="primary")
        btnOk.clicked.connect(self.accept)
        btnOk.setDefault(True)
        btns.addWidget(btnCancel)
        btns.addWidget(btnOk)
        v.addLayout(btns)
        self.url_edit.setFocus()

    def _pick(self):
        d = QFileDialog.getExistingDirectory(self, "选择保存目录", self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)


class MainWindow(QMainWindow):
    NAV = [("all", "全部任务"), ("running", "下载中"),
           ("done", "已完成"), ("error", "失败")]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Super Flash Download")
        self.resize(1060, 700)
        self.setMinimumSize(880, 560)
        _ico = os.path.join(RES_DIR, "sfd.ico")
        if os.path.exists(_ico):
            self.setWindowIcon(QIcon(_ico))

        self.tasks = {}      # tid -> 任务唯一事实源
        self.items = {}
        self.rows = {}
        self.zombies = []    # 已删除但仍需持有到线程退出的 worker
        self.next_tid = 1
        self.filter_state = "all"

        self._build_ui()
        self._load_history()

        self.timer = QTimer()
        self.timer.timeout.connect(self._tick)
        self.timer.start(500)

        self._api_srv = None
        self._api_cmds = []
        self._api_done = []
        self._api_lock = threading.Lock()
        self._start_api()

    # ================= UI =================
    def _build_ui(self) -> None:
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        side = QFrame(objectName="sidebar")
        side.setFixedWidth(190)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(14, 16, 14, 14)
        sv.setSpacing(8)
        sv.addWidget(QLabel("🌧 Super Flash Download", objectName="brand"))
        sv.addWidget(QLabel("Super Flash Download · 无广告下载", objectName="brandSub"))
        sv.addSpacing(8)
        self.nav = QListWidget(objectName="nav")
        for key, label in self.NAV:
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, key)
            self.nav.addItem(it)
        self.nav.setCurrentRow(0)
        self.nav.itemClicked.connect(self._on_nav)
        sv.addWidget(self.nav, 1)
        self.side_dir = QLabel("", objectName="brandSub")
        self.side_dir.setWordWrap(True)
        sv.addWidget(self.side_dir)
        h.addWidget(side)

        main = QWidget()
        mv = QVBoxLayout(main)
        mv.setContentsMargins(20, 16, 20, 14)
        mv.setSpacing(12)

        top = QHBoxLayout()
        self.head_title = QLabel("全部任务", objectName="headTitle")
        top.addWidget(self.head_title)
        top.addStretch()
        self.search = QLineEdit()
        self.search.setPlaceholderText("🔍 搜索任务名")
        self.search.setFixedWidth(220)
        self.search.textChanged.connect(self._apply_filter)
        btn_new = QPushButton("＋ 新建下载", objectName="primary")
        btn_new.clicked.connect(self._new_dialog)
        top.addWidget(self.search)
        top.addSpacing(8)
        top.addWidget(btn_new)
        mv.addLayout(top)

        mid = QHBoxLayout()
        mid.setSpacing(12)
        self.stat_cards = {}
        for key, label in [("speed", "实时速度"), ("tasks", "进行中"),
                           ("done", "已完成"), ("bytes", "累计下载")]:
            card = QFrame(objectName="card")
            card.setFixedHeight(56)
            cv = QVBoxLayout(card)
            cv.setContentsMargins(14, 6, 14, 6)
            val = QLabel("0", objectName="statVal")
            lab = QLabel(label, objectName="statLabel")
            cv.addWidget(val)
            cv.addWidget(lab)
            self.stat_cards[key] = val
            mid.addWidget(card, 1)
        mv.addLayout(mid)
        self.spark = SpeedSpark()
        mv.addWidget(self.spark)

        self.listw = QListWidget(objectName="tasks")
        self.listw.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.listw.setContextMenuPolicy(Qt.CustomContextMenu)
        self.listw.customContextMenuRequested.connect(self._ctx_menu)
        mv.addWidget(self.listw, 1)

        self.empty_lbl = QLabel("暂无任务 · 点右上「新建下载」开始", objectName="empty")
        self.empty_lbl.setAlignment(Qt.AlignCenter)
        mv.addWidget(self.empty_lbl)
        h.addWidget(main, 1)
        self._update_dir_label()

    def _update_dir_label(self):
        self.side_dir.setText(f"保存到\n{self._last_dir()}")

    def _last_dir(self) -> str:
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                return json.load(f).get("last_dir") or (os.path.expanduser("~") + "/Downloads")
        except Exception:
            return os.path.expanduser("~") + "/Downloads"

    def _save_last_dir(self, d: str) -> None:
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump({"last_dir": d}, f)
        except Exception:
            pass
        self._update_dir_label()

    # ================= 新建 =================
    def _new_dialog(self) -> None:
        dlg = NewTaskDialog(self, self._last_dir(), HAS_BT)
        if dlg.exec() != QDialog.Accepted:
            return
        url = dlg.url_edit.text().strip()
        if not url:
            return
        d = dlg.dir_edit.text().strip() or self._last_dir()
        self._save_last_dir(d)
        if url.startswith("magnet:") or (url.endswith(".torrent") and os.path.isfile(url)):
            if not HAS_BT:
                QMessageBox.warning(self, "Super Flash Download", "磁力/种子需要 venv 环境（桌面快捷方式已配置）")
                return
            self._spawn_bt(url, d)
            return
        if not url.startswith(("http://", "https://")):
            QMessageBox.warning(self, "Super Flash Download", "请输入 http/https 直链 / 磁力链 / .torrent 路径")
            return
        # dest 是目录，文件名由服务器 Content-Disposition 决定（URL 末段是
        # shareDownload / res.php 这类占位符时猜名字必然撞车）
        dest = d
        os.makedirs(dest, exist_ok=True)
        mirrors = [m.strip() for m in dlg.mirrors_edit.toPlainText().splitlines()
                   if m.strip()] if hasattr(dlg.mirrors_edit, "toPlainText") \
            else [m.strip() for m in dlg.mirrors_edit.text().splitlines() if m.strip()]
        proxy = (dlg.proxy_edit.text().strip() or None) if hasattr(dlg, "proxy_edit") else None
        self._spawn_http(url, dest, dlg.spin_threads.value(),
                         dlg.combo_limit.currentData(), mirrors=mirrors, proxy=proxy,
                         auto_name=True)

    # ================= 生成任务 =================
    def _new_tid(self) -> int:
        tid = self.next_tid
        self.next_tid += 1
        return tid

    def _add_row(self, tid: int, name: str, state: str, pct: int,
                 size_txt: str, note: str = "", bt_label: str = "") -> None:
        row = TaskRow(tid, name)
        row._pct = pct
        row._size_txt = size_txt
        it = QListWidgetItem()
        it.setSizeHint(row.sizeHint())
        it.setData(Qt.UserRole, tid)
        self.listw.addItem(it)
        self.listw.setItemWidget(it, row)
        self.items[tid] = it
        self.rows[tid] = row
        self._apply_row_state(tid, state, note, bt_label)

    def _spawn_http(self, url: str, dest: str, threads: int = 16, limit: int = 0,
                    tid=None, mirrors: list | None = None, proxy: str | None = None,
                    auto_name: bool = False) -> None:
        tid = tid if tid is not None else self._new_tid()
        # HF 官方源自动附上 hf-mirror 镜像：官方走代理 16 线程易被限流卡死，
        # 镜像国内直连可并行补齐慢块（mirrors 是多源并发，同块谁快谁填）
        if "huggingface.co/" in url and "hf-mirror.com" not in url:
            mirrors = list(mirrors or []) + [url.replace("huggingface.co", "hf-mirror.com", 1)]
        w = TaskWorker(tid, url, dest, threads, {}, proxy, limit, mirrors=mirrors or [],
                       auto_name=auto_name)
        w.progress.connect(self._on_http_progress)
        w.status.connect(self._on_http_status)
        w.start()
        self.zombies.append(w)
        w.finished.connect(lambda: self._reap(w))
        t = self.tasks.get(tid) or {}
        t.update({"kind": "http", "worker": w, "url": url, "dest": dest,
                  "name": os.path.basename(dest), "state": "running",
                  "done": 0, "total": 0, "ts": time.time()})
        self.tasks[tid] = t
        if tid not in self.items:
            self._add_row(tid, t["name"], "running", 0, "…")
        else:
            self._apply_row_state(tid, "running")
        self._persist()
        self._apply_filter()

    def _spawn_bt(self, source: str, save_dir: str, tid=None) -> None:
        tid = tid if tid is not None else self._new_tid()
        t = self.tasks.get(tid) or {}
        name = t.get("name") or (os.path.basename(source)[:40] or "BT 任务")

        def prog(done, total, speed_str):
            self._on_bt_progress(tid, done, total, speed_str)

        def status(text):
            self._on_bt_status(tid, text)

        def done():
            self._on_bt_done(tid)

        def err(m):
            self._on_bt_error(tid, m)

        task = xybt.BTTask(source, save_dir, port=16881 + (tid % 20),
                           rate_limit=0, on_progress=prog, on_status=status,
                           on_done=done, on_error=err)
        task.start()
        t.update({"kind": "bt", "btobj": task, "url": source, "dest": save_dir,
                  "name": name, "state": "running", "done": 0, "total": 0,
                  "bt_label": "找节点中", "ts": time.time()})
        self.tasks[tid] = t
        if tid not in self.items:
            self._add_row(tid, name, "running", 0, "…", bt_label="找节点中")
        else:
            self._apply_row_state(tid, "running", bt_label="找节点中")
        self._persist()
        self._apply_filter()

    def _reap(self, w) -> None:
        try:
            self.zombies.remove(w)
        except ValueError:
            pass

    # ================= HTTP 信号 =================
    def _on_http_progress(self, tid: int, done: float, total: float, speed: str) -> None:
        t = self.tasks.get(tid)
        row = self.rows.get(tid)
        if not t or not row or t["state"] != "running":
            return
        tk = t["worker"].task
        if tk and tk.paused.is_set():
            return
        # auto_name：引擎在 probe 后才定稿落盘文件名，这里同步显示名与落点
        if tk and tk.dest and t.get("dest") != tk.dest:
            t["dest"] = tk.dest
            real = os.path.basename(tk.dest)
            if real and t.get("name") != real:
                t["name"] = real
                row.set_name(real)
        eta = fmt_eta((total - done) / max(t["worker"]._speed, 1)) if total else "--"
        t["done"], t["total"] = done, total
        row.set_progress(done, total, speed, eta)

    def _on_http_status(self, tid: int, st: str) -> None:
        t = self.tasks.get(tid)
        if not t:
            return
        if st == "running":
            self._apply_row_state(tid, "running")
        elif st == "done":
            self._apply_row_state(tid, "done")
        else:
            t["err"] = st
            self._apply_row_state(tid, "error", note=st[:36])

    # ================= BT 信号 =================
    def _on_bt_progress(self, tid: int, done: float, total: float, speed: str) -> None:
        t = self.tasks.get(tid)
        row = self.rows.get(tid)
        if not t or not row or t["state"] != "running":
            return
        t["done"], t["total"] = done, total
        row.set_progress(done, total, speed, "--")

    def _on_bt_status(self, tid: int, text: str) -> None:
        t = self.tasks.get(tid)
        if t and t["state"] == "running":
            t["bt_label"] = text
            row = self.rows.get(tid)
            if row:
                row.set_state("running", bt_label=text)

    def _on_bt_done(self, tid: int) -> None:
        if tid in self.tasks:
            self._apply_row_state(tid, "done")

    def _on_bt_error(self, tid: int, m: str) -> None:
        t = self.tasks.get(tid)
        if not t:
            return
        t["err"] = m
        self._apply_row_state(tid, "error", note=m[:36])

    # ================= 行状态 =================
    def _apply_row_state(self, tid: int, state: str, note: str = "",
                         bt_label: str = "") -> None:
        t = self.tasks.get(tid)
        row = self.rows.get(tid)
        if not t or not row:
            return
        t["state"] = state
        if bt_label:
            t["bt_label"] = bt_label
        if row._size_txt in ("", "…") and t.get("size_str"):
            row._size_txt = t["size_str"]
        row.set_state(state, note, t.get("bt_label", ""))
        self._persist()
        self._refresh_counts()
        self._apply_filter()
        self._update_empty()

    def toggle_pause(self, tid: int) -> None:
        t = self.tasks.get(tid)
        if not t:
            return
        state = t["state"]
        if state == "running":
            if t["kind"] == "http":
                t["worker"].pause()
            else:
                t["btobj"].pause()
            self._apply_row_state(tid, "paused")
        elif state == "paused":
            if t["kind"] == "http":
                t["worker"].resume()
            else:
                t["btobj"].resume()
            self._apply_row_state(tid, "running")
        elif state == "interrupted":
            # 重新拉起：引擎按 .sfd 状态文件自动续传；BT 重加磁力
            if t["kind"] == "http":
                self._spawn_http(t["url"], t["dest"], 16, 0, tid=tid)
            else:
                self._spawn_bt(t["url"], t["dest"], tid=tid)

    def open_dir(self, tid: int) -> None:
        t = self.tasks.get(tid)
        if not t:
            return
        dest = t["dest"]
        d = dest if os.path.isdir(dest) else os.path.dirname(dest)
        if os.path.isdir(d):
            os.startfile(d)

    # ================= 删除（QListWidget 行身份独立，无错位） =================
    def delete_task(self, tid: int, with_file: bool = False) -> None:
        t = self.tasks.get(tid)
        if not t:
            return
        if t["kind"] == "http" and t.get("worker"):
            t["worker"].stop()
            self.zombies.append(t["worker"])
            t["worker"].finished.connect(lambda: self._reap(t["worker"]))
        if t["kind"] == "bt" and t.get("btobj"):
            t["btobj"].stop()
        it = self.items.pop(tid, None)
        if it is not None:
            self.listw.takeItem(self.listw.row(it))
        self.rows.pop(tid, None)
        self.tasks.pop(tid, None)
        self._persist()
        self._refresh_counts()
        self._update_empty()
        if with_file:
            dest = t.get("dest", "")
            target = dest if os.path.isdir(dest) else os.path.join(
                os.path.dirname(dest), t.get("name", ""))
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
            elif os.path.isfile(target):
                try:
                    os.remove(target)
                except OSError:
                    pass

    def _ctx_menu(self, pos) -> None:
        it = self.listw.itemAt(pos)
        if it is None:
            return
        tid = it.data(Qt.UserRole)
        t = self.tasks.get(tid, {})
        menu = QMenu(self)
        state = t.get("state", "done")
        if state in ("running", "paused", "interrupted"):
            menu.addAction("暂停" if state == "running" else "继续",
                           lambda: self.toggle_pause(tid))
        menu.addAction("打开目录", lambda: self.open_dir(tid))
        if state in ("interrupted", "error", "done"):
            menu.addAction("重新下载", lambda: self._redownload(tid))
        menu.addSeparator()
        menu.addAction("删除记录", lambda: self.delete_task(tid))
        menu.addAction("删除记录和文件", lambda: self.delete_task(tid, with_file=True))
        menu.exec(self.listw.viewport().mapToGlobal(pos))

    def _redownload(self, tid: int) -> None:
        t = self.tasks.get(tid)
        if not t:
            return
        if t["kind"] == "http":
            self._spawn_http(t["url"], t["dest"], 16, 0, tid=tid)
        else:
            self._spawn_bt(t["url"], t["dest"], tid=tid)

    # ================= 过滤 / 计数 =================
    def _on_nav(self, item) -> None:
        self.filter_state = item.data(Qt.UserRole)
        self.head_title.setText(dict(self.NAV).get(self.filter_state, "全部任务"))
        self._apply_filter()

    def _apply_filter(self) -> None:
        txt = self.search.text().strip().lower()
        for tid, t in self.tasks.items():
            it = self.items.get(tid)
            if it is None:
                continue
            ok_state = (self.filter_state == "all"
                        or (self.filter_state == "running"
                            and t["state"] in ("running", "paused"))
                        or (self.filter_state == "done" and t["state"] == "done")
                        or (self.filter_state == "error" and t["state"] == "error"))
            ok_search = not txt or txt in t["name"].lower()
            it.setHidden(not (ok_state and ok_search))
        self._update_empty()

    def _refresh_counts(self) -> None:
        counts = {"all": len(self.tasks), "running": 0, "done": 0, "error": 0}
        for t in self.tasks.values():
            if t["state"] in ("running", "paused"):
                counts["running"] += 1
            elif t["state"] == "done":
                counts["done"] += 1
            elif t["state"] == "error":
                counts["error"] += 1
        for i, (key, label) in enumerate(self.NAV):
            self.nav.item(i).setText(f"{label}  ·  {counts[key]}")

    def _update_empty(self) -> None:
        visible = sum(1 for tid, it in self.items.items()
                      if tid in self.tasks and not it.isHidden())
        self.empty_lbl.setVisible(visible == 0)

    # ================= 持久化 =================
    def _persist(self) -> None:
        try:
            data = []
            for t in sorted(self.tasks.values(), key=lambda x: -x.get("ts", 0)):
                state = t["state"]
                if state == "running":
                    state = "interrupted"   # 落盘时不可能还在下
                pct = int(t["done"] * 100 / t["total"]) if t.get("total") else 0
                data.append({"url": t["url"], "dest": t["dest"], "name": t["name"],
                             "state": state, "pct": pct, "ts": t.get("ts", time.time()),
                             "bt": t["kind"] == "bt"})
            with open(HISTORY_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _load_history(self) -> None:
        try:
            with open(HISTORY_PATH, encoding="utf-8") as f:
                items = json.load(f)
        except Exception:
            items = []
        for h in items[:80]:
            tid = self._new_tid()
            state = h.get("state") or ("done" if h.get("status") == "已完成"
                                       else "interrupted")
            if state in ("running", "paused"):
                state = "interrupted"   # 僵尸状态归一化：上次没下完就是"已中断"
            try:
                pct = min(int(h.get("pct", 0)), 100)
            except (TypeError, ValueError):
                pct = 0
            size_str = "已完成" if state == "done" else f"{pct}%"
            t = {"kind": "bt" if h.get("bt") else "http", "worker": None,
                 "btobj": None, "url": h["url"], "dest": h["dest"],
                 "name": h["name"], "state": state, "done": 0, "total": 0,
                 "ts": h.get("ts", 0)}
            self.tasks[tid] = t
            note = "" if state == "done" else "可续传"
            self._add_row(tid, h["name"], state, pct, size_str, note=note)
        self._refresh_counts()
        self._update_empty()

    # ================= 统计 =================
    def _tick(self) -> None:
        total_bps = 0.0
        for t in self.tasks.values():
            if t["state"] != "running":
                continue
            if t["kind"] == "http" and t.get("worker") and t["worker"].isRunning():
                total_bps += t["worker"]._speed
            if t["kind"] == "bt" and t.get("btobj"):
                try:
                    total_bps += t["btobj"]._speed_smooth
                except Exception:
                    pass
        running = sum(1 for t in self.tasks.values()
                      if t["state"] in ("running", "paused"))
        done = sum(1 for t in self.tasks.values() if t["state"] == "done")
        self.stat_cards["speed"].setText(fmt_speed(total_bps))
        self.stat_cards["tasks"].setText(str(running))
        self.stat_cards["done"].setText(str(done))
        self.spark.push(total_bps)

    # ================= 连接器 API（127.0.0.1:16901） =================
    def _start_api(self) -> None:
        self._api_cmds = []          # 线程安全指令队列（仅主线程消费）
        self._api_lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # 静默访问日志
                pass

            def _send(self, obj, code=200):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                # CORS：允许扩展页（chrome-extension://）跨源调用本地 API
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "*")
                # Private Network Access：HTTPS 页面 -> http://127.0.0.1 需显式许可
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                u = _urlparse(self.path)
                if u.path == "/health":
                    self._send({"ok": True, "app": "super-flash-download", "version": "1.0"})
                    return
                if u.path == "/activate":
                    with outer._api_lock:
                        outer._api_cmds.append(("activate", None))
                    self._send({"ok": True})
                    return
                if u.path == "/tasks":
                    with outer._api_lock:
                        outer._api_cmds.append(("snapshot", None))
                    snap = outer._api_wait_snapshot()
                    if snap is None:
                        self._send({"ok": False, "error": "timeout"}, 503)
                    else:
                        self._send({"ok": True, "tasks": snap})
                    return
                self._send({"ok": False, "error": "not found"}, 404)

            def do_POST(self):
                u = _urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                except Exception:
                    body = {}
                if u.path == "/tasks":
                    with outer._api_lock:
                        outer._api_cmds.append(("add", body))
                    res = outer._api_wait_result()
                    self._send(res or {"ok": False, "error": "timeout"}, 200 if res and res.get("ok") else 503)
                    return
                if u.path.endswith("/pause"):
                    tid = int(u.path.strip("/").split("/")[1])
                    with outer._api_lock:
                        outer._api_cmds.append(("pause", tid))
                    res = outer._api_wait_result()
                    self._send(res or {"ok": False, "error": "timeout"})
                    return
                if u.path.endswith("/resume"):
                    tid = int(u.path.strip("/").split("/")[1])
                    with outer._api_lock:
                        outer._api_cmds.append(("resume", tid))
                    res = outer._api_wait_result()
                    self._send(res or {"ok": False, "error": "timeout"})
                    return
                self._send({"ok": False, "error": "not found"}, 404)

            def do_DELETE(self):
                u = _urlparse(self.path)
                q = _parse_qs(u.query)
                with_file = q.get("with_file", ["0"])[0] == "1"
                tid = int(u.path.strip("/").split("/")[1])
                with outer._api_lock:
                    outer._api_cmds.append(("delete", (tid, with_file)))
                res = outer._api_wait_result()
                self._send(res or {"ok": False, "error": "timeout"})

        try:
            self._api_srv = ThreadingHTTPServer(("127.0.0.1", API_PORT), Handler)
        except OSError as e:
            print(f"API 端口 {API_PORT} 被占用，连接器不可用: {e}")
            return
        threading.Thread(target=self._api_srv.serve_forever,
                         daemon=True, name="sfd-api").start()
        # 主线程周期消费指令队列（Qt 主线程安全）
        self.api_timer = QTimer()
        self.api_timer.timeout.connect(self._drain_api)
        self.api_timer.start(120)

    def _api_wait_snapshot(self, timeout: float = 2.0):
        import time as _t
        t0 = _t.time()
        while _t.time() - t0 < timeout:
            with self._api_lock:
                for i, (kind, payload) in enumerate(self._api_done):
                    if kind == "snapshot":
                        self._api_done.pop(i)
                        return payload
            _t.sleep(0.05)
        return None

    def _api_wait_result(self, timeout: float = 2.0):
        import time as _t
        t0 = _t.time()
        while _t.time() - t0 < timeout:
            with self._api_lock:
                if self._api_done:
                    return self._api_done.pop(0)[1]
            _t.sleep(0.05)
        return None

    def _drain_api(self) -> None:
        """主线程消费连接器指令。"""
        while True:
            with self._api_lock:
                if not self._api_cmds:
                    return
                kind, payload = self._api_cmds.pop(0)
            try:
                if kind == "snapshot":
                    snap = [self._task_api_view(tid) for tid in self.tasks]
                    with self._api_lock:
                        self._api_done.append(("snapshot", snap))
                elif kind == "add":
                    res = self._api_add(payload)
                    with self._api_lock:
                        self._api_done.append(("result", res))
                elif kind in ("pause", "resume"):
                    tid = payload
                    if tid not in self.tasks:
                        res = {"ok": False, "error": f"任务 {tid} 不存在"}
                    else:
                        st = self.tasks[tid]["state"]
                        if kind == "pause" and st == "running":
                            self.toggle_pause(tid)
                        elif kind == "resume" and st in ("paused", "interrupted"):
                            self.toggle_pause(tid)
                        res = {"ok": True, "tid": tid, "state": self.tasks[tid]["state"]}
                    with self._api_lock:
                        self._api_done.append(("result", res))
                elif kind == "activate":
                    try:
                        w = self.window()
                        w.showNormal()
                        w.raise_()
                        w.activateWindow()
                        res = {"ok": True, "activated": True}
                    except Exception as e:
                        res = {"ok": False, "error": str(e)}
                    with self._api_lock:
                        self._api_done.append(("result", res))
                elif kind == "delete":
                    tid, with_file = payload
                    if tid not in self.tasks:
                        res = {"ok": False, "error": f"任务 {tid} 不存在"}
                    else:
                        self.delete_task(tid, with_file=with_file)
                        res = {"ok": True, "tid": tid, "deleted": True}
                    with self._api_lock:
                        self._api_done.append(("result", res))
            except Exception as e:
                with self._api_lock:
                    self._api_done.append(("result", {"ok": False, "error": str(e)}))

    def _api_add(self, body: dict) -> dict:
        url = (body.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "url 必填"}
        dest_dir = body.get("dest") or self._last_dir()
        os.makedirs(dest_dir, exist_ok=True)
        threads = int(body.get("threads") or 16)
        limit = int(body.get("limit") or 0)
        proxy = (body.get("proxy") or "").strip() or None
        mirrors = [str(m).strip() for m in (body.get("mirrors") or []) if m and str(m).strip()]
        # API 的 dest 语义是"保存目录"，文件名由服务器 Content-Disposition 决定。
        # 这里必须默认 True：否则引擎会把目录当成文件路径 open → Permission denied。
        auto_name = bool(body.get("auto_name", True))
        if url.startswith("magnet:") or (url.endswith(".torrent") and os.path.isfile(url)):
            if not HAS_BT:
                return {"ok": False, "error": "当前解释器缺 libtorrent"}
            self._spawn_bt(url, dest_dir)
            tid = max(self.tasks)
            return {"ok": True, "tid": tid, "name": self.tasks[tid]["name"]}
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": "仅支持 http/https/magnet/.torrent"}
        # dest 是目录，文件名由服务器 Content-Disposition 决定（URL 末段是
        # shareDownload / res.php 这类占位符时猜名字必然撞车）
        self._spawn_http(url, dest_dir, threads, limit, mirrors=mirrors,
                         proxy=proxy, auto_name=auto_name)
        tid = max(self.tasks)
        return {"ok": True, "tid": tid, "name": self.tasks[tid]["name"]}

    def _task_api_view(self, tid: int) -> dict:
        t = self.tasks[tid]
        done, total = t.get("done") or 0, t.get("total") or 0
        pct = int(done * 100 / total) if total else 0
        speed = ""
        if t["state"] == "running":
            if t["kind"] == "http" and t.get("worker"):
                speed = fmt_speed(t["worker"]._speed)
            elif t["kind"] == "bt" and t.get("btobj"):
                speed = fmt_speed(getattr(t["btobj"], "_speed_smooth", 0))
        size_str = (f"{fmt_size(done)} / {fmt_size(total)}" if total
                    else fmt_size(done) if done else "—")
        return {"tid": tid, "name": t["name"], "kind": t["kind"],
                "state": t["state"],
                "state_label": STATE_META.get(t["state"], ("", t["state"]))[1],
                "pct": pct, "done": done, "total": total,
                "size_str": size_str, "speed": speed,
                "err": t.get("err", ""),
                "dest": t["dest"], "url": t["url"]}

    def closeEvent(self, ev) -> None:
        if getattr(self, "_api_srv", None):
            try:
                self._api_srv.shutdown()
            except Exception:
                pass
        for t in self.tasks.values():
            try:
                if t["kind"] == "http" and t.get("worker"):
                    t["worker"].stop()
                if t["kind"] == "bt" and t.get("btobj"):
                    t["btobj"].stop()
            except Exception:
                pass
        super().closeEvent(ev)


def _acquire_single_instance() -> bool:
    """Windows 命名互斥体单实例锁：已是第二实例返回 False。
    互斥体随进程退出由内核回收，无需显式 CloseHandle。"""
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenMutexW.restype = ctypes.c_void_p
    k32.OpenMutexW.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    SYNCHRONIZE = 0x00100000
    # 不能用 CreateMutexW + get_last_error()==183 判断：PyInstaller onefile 下
    # ctypes.get_last_error() 读不到真实错误码（实测第二实例被误判成第一实例）。
    # 改为先 OpenMutexW 探测，能打开就说明已有实例在跑，不依赖 last error。
    if k32.OpenMutexW(SYNCHRONIZE, False, "SFD_SingleInstance_Mutex"):
        return False          # 句柄随进程退出自动回收
    return bool(k32.CreateMutexW(None, False, "SFD_SingleInstance_Mutex"))


def main() -> int:
    if not _acquire_single_instance():
        # 先唤出已开的窗口（API 在线时生效），再弹提示
        try:
            rq = urllib.request.Request(
                f"http://127.0.0.1:{API_PORT}/activate",
                data=b"{}", method="POST",
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(rq, timeout=1)
        except Exception:
            pass
        if os.environ.get("SFD_NO_MSGBOX") != "1":   # 测试钩子：自动化环境跳过弹窗
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, "Super Flash Download已在运行，已为你唤出主窗口。\n（如未显示请查看任务栏）",
                "Super Flash Download", 0x40)
        return 3
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    _ico = os.path.join(RES_DIR, "sfd.ico")
    if os.path.exists(_ico):
        app.setWindowIcon(QIcon(_ico))
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

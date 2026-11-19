# -*- coding: utf-8 -*-
"""filename_from_headers / auto_name 落盘命名 —— 回归测试

问题：URL 末段是占位符（/shareDownload、/res.php）时，从 URL 猜的文件名必然错，
多个文件会撞成同一个名字。正确做法是取服务器 Content-Disposition 声明的真名。
"""
import os
import sys
import urllib.parse

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import xyengine as eng


def test_rfc5987_utf8_filename():
    """RFC 5987: filename*=UTF-8''<百分号编码>"""
    cd = ("attachment; filename*=UTF-8''%E8%BD%AF%E4%BB%B6%E8%AE%B8%E5%8F%AF"
          "%E5%8D%8F%E8%AE%AE.txt")
    got = eng.filename_from_headers({"Content-Disposition": cd})
    assert got == "软件许可协议.txt", got


def test_rfc5987_with_language_tag():
    """带语言段: filename*=UTF-8'zh-CN'<value>"""
    cd = "attachment; filename*=UTF-8'zh-CN'%E4%B8%AD%E6%96%87.rar"
    got = eng.filename_from_headers({"Content-Disposition": cd})
    assert got == "中文.rar", got


def test_plain_quoted_filename():
    cd = 'attachment; filename="PLMC-UAT-UTIL.rar"'
    got = eng.filename_from_headers({"Content-Disposition": cd})
    assert got == "PLMC-UAT-UTIL.rar", got


def test_plain_unquoted_filename():
    cd = "attachment; filename=app.tar.gz"
    got = eng.filename_from_headers({"Content-Disposition": cd})
    assert got == "app.tar.gz", got


def test_no_content_disposition():
    assert eng.filename_from_headers({}) == ""
    assert eng.filename_from_headers(None) == ""


def test_filename_with_space_kept():
    """文件名含空格: 'PLM CLoud专业版升级高级版.pptx'"""
    cd = ("attachment; filename*=UTF-8''PLM%20CLoud%E4%B8%93%E4%B8%9A%E7%89%88"
          "%E5%8D%87%E7%BA%A7%E9%AB%98%E7%BA%A7%E7%89%88.pptx")
    got = eng.filename_from_headers({"Content-Disposition": cd})
    assert got == "PLM CLoud专业版升级高级版.pptx", got


def _mk_task(url, dest, auto_name):
    """造一个只够 _prepare() 用的 Task 骨架。"""
    import threading
    t = eng.Task.__new__(eng.Task)
    t.url = url
    t.headers = {}
    t.proxy = None
    t.mirrors = []
    t.auto_name = auto_name
    t.dest = dest
    t.threads = 4
    t.rate_limit = 0
    t.retries = 5
    t.stop_flag = threading.Event()
    t.paused = threading.Event()
    t._inflight = {}
    t._inflight_lock = threading.Lock()
    t._local = threading.local()
    t._last_save = 0.0
    t.state = {}
    t.on_progress = t.on_done = t.on_error = None
    t.done_bytes = 0
    t.prog_lock = threading.Lock()
    return t


def test_auto_name_rewrites_dest_to_server_name(tmp_path):
    """auto_name=True 时，落盘名必须是服务器声明的真名，而不是 URL 末段。"""
    dest_dir = str(tmp_path)
    t = _mk_task("https://example.com/s/api/shareDownload?fid=1",
                 dest_dir, True)

    # 假 probe：URL 末段是 shareDownload（占位符），CD 给真名
    orig_probe = eng.probe
    eng.probe = lambda url, headers=None, proxy=None: (
        1024, True, "示例文档.rar")
    try:
        eng.Task._prepare(t)
    finally:
        eng.probe = orig_probe
    assert os.path.basename(t.dest) == "示例文档.rar", t.dest
    assert os.path.dirname(t.dest) == dest_dir, t.dest


def test_auto_name_false_keeps_url_guess(tmp_path):
    """auto_name=False 时行为不变（老逻辑：URL 末段）"""
    dest_dir = str(tmp_path)
    guessed = os.path.join(dest_dir, "shareDownload")
    t = _mk_task("https://example.com/s/api/shareDownload?fid=1",
                 guessed, False)

    orig_probe = eng.probe
    eng.probe = lambda url, headers=None, proxy=None: (
        1024, True, "示例文档.rar")
    try:
        eng.Task._prepare(t)
    finally:
        eng.probe = orig_probe
    assert os.path.basename(t.dest) == "shareDownload", t.dest

def test_ua_is_browser_like():
    """UA 必须像真实浏览器。

    部分下载站（实测某网盘）只对带 AppleWebKit/Gecko 的 UA 返回
    RFC 5987 编码的 Content-Disposition；自造 UA（Super Flash Download/2.0、curl/8.4.0）
    会让服务端把中文文件名替换成空格，落盘成 "CAD      .rar"，
    信息不可恢复。
    """
    ua = eng.UA
    assert "AppleWebKit" in ua or "Gecko" in ua, ua
    assert "Super Flash Download" not in ua, ua

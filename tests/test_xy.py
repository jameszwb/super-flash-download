import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from xy import split_ranges, load_state, save_state, state_path  # noqa: E402


def test_split_ranges_cover_every_byte():
    size = 10_000_000
    parts = split_ranges(size, 8)
    assert parts[0][0] == 0
    assert parts[-1][1] == size - 1
    for a, b in zip(parts, parts[1:]):
        assert b[0] == a[1] + 1, "分片之间不能有空洞"
    assert sum(e - s + 1 for s, e in parts) == size


def test_split_ranges_single_thread_is_whole_file():
    assert split_ranges(5000, 1) == [(0, 4999)]


def test_state_roundtrip(tmp_path):
    f = tmp_path / "a.bin"
    st = {"url": "http://x", "size": 100, "done": [[0, 49]]}
    save_state(str(f), st)
    assert load_state(str(f), "http://x", 100) == st


def test_state_rejected_on_url_or_size_mismatch(tmp_path):
    f = tmp_path / "a.bin"
    save_state(str(f), {"url": "http://x", "size": 100, "done": [[0, 49]]})
    assert load_state(str(f), "http://y", 100)["done"] == []
    assert load_state(str(f), "http://x", 200)["done"] == []

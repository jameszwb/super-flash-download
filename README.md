# Super Flash Download

A multi-connection downloader for Windows — work-stealing engine, byte-precise resume, and a local REST API that lets scripts or AI agents drive the GUI.

**The engine has zero third-party dependencies.** It is pure Python standard library.
The GUI needs PySide6. BitTorrent support is optional and needs libtorrent (Python 3.13).

---

## Why this exists

Most desktop download managers either bundle a browser engine, phone home, or hide the
engine behind a proprietary binary. This one is ~2,600 lines you can read in an afternoon:

- The **engine** (`xyengine.py`) splits a file into byte ranges, hands them to a pool of
  threads that *steal* work from each other, and commits progress to disk before counting
  a byte as done. No "99% forever", no zero-filled files that report 100%.
- The **GUI** (`xygui.py`) is PySide6: category sidebar, card-style task rows, live speed
  curve, per-task controls.
- The **connector** (`xyc.py`) is a CLI client for the GUI's local REST API on
  `127.0.0.1:16901`, so `curl`, a shell script, or an LLM agent can queue downloads
  without touching the UI.

> `xy` in the module filenames is the original internal codename. The product name is
> **Super Flash Download**; the prefix was kept to avoid churn in imports and history.

---

## Features

| Area | What you get |
|---|---|
| **Engine** | Work-stealing dynamic chunking (8 MB chunks, 256 KB tail) — fast threads automatically take work from slow ones |
| **Connections** | Keep-alive connection pool, one long-lived socket per thread |
| **Resume** | `.sfd` state file stores completed ranges plus in-flight cursors; a killed task resumes byte-exactly |
| **Multi-source** | Pass several mirrors for the same file; the engine probes each (size match + Range support), drops dead mirrors, and rotates threads across the survivors |
| **Proxy** | Real HTTPS `CONNECT` tunnelling per worker (not just for the probe request), with optional `user:pass` |
| **Filenames** | Parses RFC 5987 `filename*=UTF-8''…` before plain `filename=`, so server-declared names win over URL basenames |
| **Redirects** | Follows 301/302/303/307/308 manually, preserving `Range` so a redirect never restarts a chunk |
| **BitTorrent** | Magnet links and `.torrent` files via libtorrent, same task model as HTTP |
| **Limits** | Five speed presets from 1 MB/s to unlimited |
| **Single instance** | Windows named mutex; a second launch exits with code 3 and raises the existing window |

---

## Install

```bash
git clone https://github.com/jameszwb/super-flash-download.git
cd super-flash-download

# engine + CLI: nothing to install
python xyengine.py --help

# GUI
pip install PySide6
python xygui.py

# optional: BitTorrent (needs Python 3.13 — that is what libtorrent ships wheels for)
pip install libtorrent
```

### Requirements

- Windows 10/11 (the GUI and single-instance logic are Windows-specific; the engine is portable)
- Python 3.10+ for the engine, 3.13 for BitTorrent
- PySide6 for the GUI only

---

## Usage

### GUI

```bash
python xygui.py
```

Paste a URL or a magnet link, pick a folder, done.

### Engine CLI

```bash
# 16 threads, 5 MB/s cap
python xyengine.py https://example.com/file.zip -n 16 --limit 5242880

# several mirrors for the same file
python xyengine.py https://mirror-a/file.zip \
    --mirror https://mirror-b/file.zip \
    --mirror https://mirror-c/file.zip

# through an HTTP proxy
python xyengine.py https://example.com/file.zip --proxy http://127.0.0.1:7890
```

### Connector API (drive the GUI from a script)

The GUI exposes a local-only REST API on port 16901 while it runs.

```bash
python xyc.py add <url> -o D:\Downloads   # queue a task (http / magnet / .torrent)
python xyc.py list                        # live status, progress, speed
python xyc.py info <task-id>              # one task, JSON
python xyc.py pause|resume <task-id|all>
python xyc.py delete <task-id|all> [--with-file]
python xyc.py health                      # is the GUI up?
```

If the GUI is not running, `xyc.py` launches it and polls until the API answers —
no manual clicking required.

---

## Architecture

| File | Role | Lines |
|---|---|---|
| `xyengine.py` | Work-stealing engine, `BlockQueue` accounting, probe, resume state | ~760 |
| `xygui.py` | PySide6 GUI + embedded REST API | ~1,230 |
| `xyc.py` | CLI connector for the REST API | ~240 |
| `xybt.py` | libtorrent backend, same `Task` interface as HTTP | ~175 |
| `xy.py` | Standalone single-file downloader (no GUI, no deps) | ~240 |

**Accounting rule that the engine is built around:** `steal()` only *issues* work, it never
touches the byte counters. `commit()` is the single place where bytes are counted, and it
runs *after* the write lands. Anything that reports progress early is the bug behind
"100% but the file is empty".

---

## Measured numbers

Real runs on Windows 11, a home fibre line, from the project's own history:

| Scenario | Result |
|---|---|
| Node.js zip, 28.3 MB, 16 threads | SHA-256 matches the official `SHASUMS256.txt` |
| Same file, single connection | **6.5× faster** with chunking |
| 1 MB chunks, 16 threads | 6.0 MB/s |
| 8 MB chunks, 4 threads | **21.2 MB/s** — chunk size dominates thread count |
| HTTP proxy, 33.6 MB | 394 KB/s before the tunnel fix → **6.6 MB/s** after |
| Mirror pool, 356 MB | ~10 s (~17.9 MB/s) on a fast domestic mirror |
| Pause then resume | Net gain 0 bytes while paused; SHA-256 intact after resume |
| BitTorrent, Sintel 123 MB | FINISHED; ramp-up 19 KB/s → 1.1 MB/s after peer handshake |

---

## Known limits

- **Windows-centric.** Engine is portable; the GUI, named mutex, and `.lnk`-style launch
  flow are Windows-specific.
- **No Range support on the server → single-threaded, no resume.** That is a protocol
  limit, not a bug.
- **No ED2K / thunder:// support.** `thunder://` links can only open a third-party client.
- **`.sfd` resume files are not portable across different file sizes.** If the remote file
  changes size, the state file is discarded rather than splicing a corrupt file.
- **BitTorrent and the GUI must run on Python 3.13** — libtorrent does not ship wheels for
  every version.

---

## License

MIT. See [LICENSE](LICENSE).

---

## 中文说明

[README.zh-CN.md](README.zh-CN.md)
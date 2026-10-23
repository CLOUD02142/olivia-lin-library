# -*- coding: utf-8 -*-
"""
app.py —— 林离曲库 v2 的本地服务

双击 启动.bat 后：
  1. 起一个只监听 127.0.0.1 的小服务
  2. 用默认浏览器打开界面
  3. 界面里能扫缓存、一键导出、手动/自动导入音乐、直接播放

不需要联网、不需要命令行、不需要装数据库。
"""

import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import core

APP_DIR = Path(__file__).resolve().parent
UI_FILE = APP_DIR / "ui.html"
HOST = "127.0.0.1"
PORT_RANGE = range(8765, 8801)

MEDIA_EXTS = core.AUDIO_EXTS | {".jpg", ".jpeg", ".png", ".webp", ".mp4"}

# ---------------------------------------------------------------- 任务（后台线程 + 进度）

JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_SEQ = [0]


def new_job(kind):
    with JOBS_LOCK:
        JOB_SEQ[0] += 1
        jid = f"j{JOB_SEQ[0]}"
        JOBS[jid] = {"id": jid, "kind": kind, "status": "running", "done": 0,
                     "total": 0, "message": "准备中…", "logs": [], "error": None,
                     "result": None, "t": time.time()}
        for k in list(JOBS)[:-12]:
            JOBS.pop(k, None)
        return jid


def job_update(jid, **kw):
    with JOBS_LOCK:
        j = JOBS.get(jid)
        if j:
            j.update(kw)


def job_log(jid, line):
    with JOBS_LOCK:
        j = JOBS.get(jid)
        if j:
            j["logs"].append(line)
            j["logs"] = j["logs"][-200:]


def job_get(jid):
    with JOBS_LOCK:
        j = JOBS.get(jid)
        return dict(j) if j else None


def run_async(jid, fn):
    def wrapper():
        try:
            fn(jid)
            job_update(jid, status="done", message="完成")
        except Exception as e:
            traceback.print_exc()
            job_update(jid, status="error", message=str(e), error=str(e))
    threading.Thread(target=wrapper, daemon=True).start()


# ---------------------------------------------------------------- 各项任务实现

def job_extract(jid, want_mp3=True):
    cfg = core.load_config()
    ffmpeg = core.find_ffmpeg(cfg)
    if not ffmpeg:
        raise RuntimeError("缺少 ffmpeg，请先在界面里点「下载 ffmpeg」")
    videos = core.scan_videos()
    catalog = core.load_catalog()
    todo = []
    for song in catalog["songs"]:
        m4a = core.dir_m4a(cfg) / f"{core.track_stem(song)}.m4a"
        if not m4a.is_file() and song["key"] in videos:
            todo.append((song, videos[song["key"]][1]))
    job_update(jid, total=len(todo), message=f"待导出 {len(todo)} 首")
    for i, (song, video) in enumerate(todo, start=1):
        job_update(jid, done=i - 1, message=f"正在导出 {song['title']} · {song['artist']}")
        try:
            core.extract_song(song, video, cfg, ffmpeg, want_mp3=want_mp3)
            job_log(jid, f"✓ {song['title']} · {song['artist']}")
        except Exception as e:
            job_log(jid, f"✗ {song['title']}：{e}")
        job_update(jid, done=i)
    job_update(jid, result={"extracted": len(todo)})


def job_import(jid, path=None):
    cfg = core.load_config()
    if path:
        p = core.normalize_dir(path)
        if not Path(p).is_dir():
            raise RuntimeError(f"目录不存在：{p}")
        if p not in cfg["importDirs"]:
            cfg["importDirs"].append(p)
            core.save_config(cfg)
        job_update(jid, message=f"正在读取 {p}")
    dirs = cfg["importDirs"]
    files = core.scan_audio(dirs, exclude=[core.out_dir(cfg), APP_DIR])
    job_update(jid, total=len(files), message=f"共 {len(files)} 个音频，正在读取标签…")
    ffprobe = core.ffprobe_of(core.find_ffmpeg(cfg))
    for i, f in enumerate(files, start=1):
        if i % 15 == 0 or i == len(files):
            job_update(jid, done=i, message=f"读取标签 {i}/{len(files)}")
        core.probe_meta(f, ffprobe)
    job_update(jid, done=len(files), result={"files": len(files)})


def job_autoscan(jid, mode="quick"):
    job_update(jid, message="开始扫描…")
    cfg = core.load_config()

    def cb(msg, pct):
        job_update(jid, message=msg, done=int(pct), total=100)

    ffprobe = core.ffprobe_of(core.find_ffmpeg(cfg))
    exclude = [core.out_dir(cfg), APP_DIR, core.game_cache_dir(), core.image_cache_dir()]
    files = core.autoscan(progress=cb, mode=mode, exclude=exclude, ffprobe=ffprobe, min_sec=20)

    known = set()
    for d in cfg["importDirs"]:
        for f in core.scan_audio([d]):
            known.add(str(f).lower())
    extra = [x for x in (cfg.get("extraFiles") or []) if Path(x).is_file()]
    extra_set = {str(Path(x)).lower() for x in extra}

    # 按目录聚合，一个目录里像样的曲子够多就整目录收，否则单独记文件
    by_dir = {}
    for f in files:
        by_dir.setdefault(str(f.parent), []).append(f)

    added_dirs, added_files = 0, 0
    for parent, siblings in by_dir.items():
        fresh = [f for f in siblings if str(f).lower() not in known]
        if not fresh:
            continue
        if len(fresh) >= 3:
            if parent not in cfg["importDirs"]:
                cfg["importDirs"].append(parent)
                added_dirs += 1
            for f in fresh:
                known.add(str(f).lower())
            added_files += len(fresh)
        else:
            for f in fresh:
                s = str(f)
                if s.lower() not in extra_set:
                    extra.append(s)
                    extra_set.add(s.lower())
                    added_files += 1

    cfg["extraFiles"] = extra
    core.save_config(cfg)
    job_update(jid, done=100, total=100,
               message=f"扫描完成：新增 {added_files} 个音频（{added_dirs} 个目录）",
               result={"found": len(files), "added": added_files, "dirs": added_dirs})


def job_ffmpeg(jid):
    def cb(msg, pct):
        job_update(jid, message=msg, done=pct, total=100)
    exe = core.download_ffmpeg(progress=cb)
    cfg = core.load_config()
    cfg["ffmpeg"] = exe
    core.save_config(cfg)
    job_update(jid, result={"ffmpeg": exe})


# ---------------------------------------------------------------- 文件选择对话框

PICK_DIR_SCRIPT = r"""
import tkinter as tk
from tkinter import filedialog
root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
p = filedialog.askdirectory(title='选择音乐文件夹')
root.destroy()
print(p or '')
"""

PICK_FILES_SCRIPT = r"""
import tkinter as tk
from tkinter import filedialog
root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
ps = filedialog.askopenfilenames(title='选择音乐文件',
    filetypes=[('音频文件','*.mp3 *.flac *.m4a *.aac *.wav *.ogg *.opus *.ape *.wma'),('所有文件','*.*')])
root.destroy()
print('\n'.join(ps))
"""


def run_picker(mode):
    script = PICK_FILES_SCRIPT if mode == "files" else PICK_DIR_SCRIPT
    py = sys.executable
    if os.name == "nt" and py.lower().endswith("pythonw.exe"):
        cand = str(Path(py).with_name("python.exe"))
        if Path(cand).is_file():
            py = cand
    try:
        r = subprocess.run([py, "-c", script], capture_output=True, text=True, timeout=300)
        out = (r.stdout or "").strip()
        if not out:
            return []
        return [x for x in out.splitlines() if x.strip()]
    except Exception:
        return []


def open_in_explorer(path):
    p = Path(path)
    if not p.exists():
        return False
    if os.name == "nt":
        os.startfile(str(p))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])
    return True


# ---------------------------------------------------------------- HTTP

def allowed_media(path):
    """只允许读曲库目录、导入目录、游戏缓存、封面缓存里的媒体文件。"""
    try:
        p = Path(path).resolve()
    except Exception:
        return None
    if not p.is_file() or p.suffix.lower() not in MEDIA_EXTS:
        return None
    cfg = core.load_config()
    roots = [core.out_dir(cfg), core.game_cache_dir(), core.image_cache_dir(), APP_DIR]
    roots += [Path(d) for d in cfg["importDirs"]]
    for r in roots:
        try:
            r = r.resolve()
            if p == r or r in p.parents:
                return p
        except Exception:
            continue
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "LinLiLibrary/2.0"

    def log_message(self, fmt, *args):
        pass

    # ---------- 工具 ----------
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    # ---------- GET ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            if not UI_FILE.is_file():
                return self._send(500, "ui.html 缺失", "text/plain; charset=utf-8")
            return self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")

        if path == "/api/state":
            try:
                return self._json({"ok": True, "data": core.build_state()})
            except Exception as e:
                traceback.print_exc()
                return self._json({"ok": False, "error": str(e)}, 500)

        if path == "/api/job":
            j = job_get((qs.get("id") or [""])[0])
            if not j:
                return self._json({"ok": False, "error": "任务不存在"}, 404)
            return self._json({"ok": True, "data": j})

        if path == "/api/ping":
            return self._json({"ok": True, "v": 2})

        if path == "/media":
            raw = (qs.get("p") or [""])[0]
            target = allowed_media(raw)
            if not target:
                return self._send(403, "forbidden", "text/plain; charset=utf-8")
            return self._stream(target)

        return self._send(404, "not found", "text/plain; charset=utf-8")

    # ---------- 媒体流（支持 Range，能拖动进度） ----------
    def _stream(self, p: Path):
        size = p.stat().st_size
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                elif not m.group(1):
                    start = max(0, size - 65536)
                code = 206
        length = max(0, end - start + 1)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(p, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------- POST ----------
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self._body()

        try:
            if path == "/api/refresh":
                return self._json({"ok": True, "data": core.build_state()})

            if path == "/api/job":
                kind = body.get("type")
                if kind == "extract":
                    jid = new_job("extract")
                    run_async(jid, lambda j: job_extract(j, body.get("mp3", True)))
                    return self._json({"ok": True, "id": jid})
                if kind == "import":
                    jid = new_job("import")
                    run_async(jid, lambda j: job_import(j, body.get("path")))
                    return self._json({"ok": True, "id": jid})
                if kind == "autoscan":
                    jid = new_job("autoscan")
                    run_async(jid, lambda j: job_autoscan(j, body.get("mode", "quick")))
                    return self._json({"ok": True, "id": jid})
                if kind == "ffmpeg":
                    jid = new_job("ffmpeg")
                    run_async(jid, job_ffmpeg)
                    return self._json({"ok": True, "id": jid})
                return self._json({"ok": False, "error": "未知任务"}, 400)

            if path == "/api/pick":
                return self._json({"ok": True, "data": run_picker(body.get("mode", "dir"))})

            if path == "/api/importdir":
                cfg = core.load_config()
                p = core.normalize_dir(body.get("path"))
                if body.get("remove"):
                    cfg["importDirs"] = [d for d in cfg["importDirs"]
                                         if Path(d).resolve() != Path(p).resolve()]
                    core.save_config(cfg)
                    return self._json({"ok": True, "data": cfg["importDirs"]})
                if not p or not Path(p).is_dir():
                    return self._json({"ok": False, "error": "目录不存在"}, 400)
                if Path(p).resolve() not in [Path(d).resolve() for d in cfg["importDirs"]]:
                    cfg["importDirs"].append(p)
                    core.save_config(cfg)
                return self._json({"ok": True, "data": cfg["importDirs"]})

            if path == "/api/setout":
                p = (body.get("path") or "").strip()
                if not p:
                    return self._json({"ok": False, "error": "路径为空"}, 400)
                cfg = core.load_config()
                cfg["out"] = p
                core.save_config(cfg)
                Path(p).mkdir(parents=True, exist_ok=True)
                return self._json({"ok": True, "data": p})

            if path == "/api/open":
                p = body.get("path") or str(core.out_dir())
                return self._json({"ok": open_in_explorer(p)})

            return self._json({"ok": False, "error": "not found"}, 404)

        except Exception as e:
            traceback.print_exc()
            return self._json({"ok": False, "error": str(e)}, 500)


class LocalServer(ThreadingHTTPServer):
    """Windows 下默认的 SO_REUSEADDR 会让多个进程抢同一个端口，
    后启动的实例会"静默劫持"先启动的。这里改成独占。"""
    allow_reuse_address = False
    daemon_threads = True


def _lock_file():
    return core.DATA_DIR / "server.json"


def read_lock():
    """上次跑在哪个端口。有锁文件就只探这一个，别去扫 80 个端口。"""
    try:
        d = json.loads(_lock_file().read_text(encoding="utf-8"))
        return int(d["port"])
    except Exception:
        return None


def write_lock(port):
    try:
        core.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _lock_file().write_text(json.dumps({"port": port, "pid": os.getpid()}),
                                encoding="utf-8")
    except Exception:
        pass


def clear_lock():
    try:
        _lock_file().unlink(missing_ok=True)
    except Exception:
        pass


def ping_port(port, timeout=1.2):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://{HOST}:{port}/api/ping", timeout=timeout) as r:
            return b'"v"' in r.read(200)
    except Exception:
        return False


def find_running():
    """先看锁文件；没有就退化成只看前几个端口，绝不扫满 80 个。"""
    port = read_lock()
    if port:
        if ping_port(port):
            return port
        clear_lock()
    for p in list(PORT_RANGE)[:4]:
        if ping_port(p, timeout=0.4):
            return p
    return None


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)   # 窗口里要能立刻看到地址
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    core.load_config()

    running = find_running()
    if running:
        url = f"http://{HOST}:{running}/"
        print("=" * 52)
        print("  林离 · 书房 已经在运行了")
        print(f"  界面地址：{url}")
        print("=" * 52)
        webbrowser.open(url)
        return

    srv = None
    for port in PORT_RANGE:
        try:
            srv = LocalServer((HOST, port), Handler)
            break
        except OSError:
            continue
    if not srv:
        print("端口 8765-8800 全被占用，起不来。")
        sys.exit(1)

    url = f"http://{HOST}:{srv.server_address[1]}/"
    write_lock(srv.server_address[1])
    print("=" * 52)
    print("  林离 · 书房")
    print(f"  界面地址：{url}")
    print("  关掉这个窗口即停止服务")
    print("=" * 52)

    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        clear_lock()
        srv.server_close()


if __name__ == "__main__":
    main()

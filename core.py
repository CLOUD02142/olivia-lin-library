# -*- coding: utf-8 -*-
"""
core.py —— 林离曲库的引擎层

负责：找 ffmpeg、读曲目表、扫演奏视频、拆音轨、扫本地音乐、维护配置。
不关心怎么被调用（app.py 的网页界面 / export.py 的命令行 都走这一层）。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CATALOG_FILE = APP_DIR / "曲目表.json"

DATA_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "OliviaLinLibrary"
CONFIG_FILE = DATA_DIR / "config.json"
PROBE_CACHE = DATA_DIR / "probe-cache.json"
FFMPEG_DIR = APP_DIR / "ffmpeg"

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus", ".ape", ".wma"}
PLAYABLE = {"mp3", "aac", "mp4a", "vorbis", "opus", "flac", "alac", "pcm_s16le", "pcm_s24le", "pcm_f32le"}

VIDEO_RE = re.compile(r"^(?P<key>.+?)_TOD(?P<tod>\d{3,4})_NI(?:_L)?$", re.I)
PREFERRED_TOD = ["1200", "1730", "2000"]
ILLEGAL = re.compile(r'[\\/:*?"<>|]')

FFMPEG_URLS = [
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
]


# ---------------------------------------------------------------- 基础

def safe(name):
    return ILLEGAL.sub("_", str(name)).strip().rstrip(".")


def first_existing(paths):
    for p in paths:
        if p and Path(p).is_file():
            return str(Path(p))
    return None


def find_ffmpeg(cfg=None):
    """找 ffmpeg：用户配置 > 本地下载 > PATH > 常见路径。"""
    cfg = cfg or load_config()
    cands = [cfg.get("ffmpeg"), FFMPEG_DIR / "ffmpeg.exe", FFMPEG_DIR / "bin" / "ffmpeg.exe"]
    hit = first_existing(cands)
    if hit:
        return hit
    w = shutil.which("ffmpeg")
    if w:
        return w
    for p in [r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
              "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"]:
        if Path(p).is_file():
            return p
    return None


def ffprobe_of(ffmpeg):
    if not ffmpeg:
        return None
    p = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if p.is_file():
        return str(p)
    return shutil.which("ffprobe")


def download_ffmpeg(progress=None):
    """下个静态版 ffmpeg 解压到 ./ffmpeg/。progress(text, pct) 回调。"""
    FFMPEG_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = FFMPEG_DIR / "ffmpeg.zip"
    last_err = None
    for url in FFMPEG_URLS:
        try:
            if progress:
                progress("正在下载 ffmpeg（约 80–150 MB）…", 0)
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as r, open(zip_path, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if progress and total:
                        progress(f"正在下载 ffmpeg… {got // 1048576}MB / {total // 1048576}MB",
                                 int(got * 90 / total))
            if progress:
                progress("正在解压…", 92)
            with zipfile.ZipFile(zip_path) as z:
                for name in z.namelist():
                    base = Path(name).name
                    if base in ("ffmpeg.exe", "ffprobe.exe"):
                        with z.open(name) as src, open(FFMPEG_DIR / base, "wb") as dst:
                            shutil.copyfileobj(src, dst)
            zip_path.unlink(missing_ok=True)
            exe = FFMPEG_DIR / "ffmpeg.exe"
            if exe.is_file():
                if progress:
                    progress("ffmpeg 就绪", 100)
                return str(exe)
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"下载 ffmpeg 失败：{last_err}")


# ---------------------------------------------------------------- 配置

def default_config():
    return {
        "out": str(Path.home() / "Music" / "林离曲库"),
        "importDirs": [],
        "ffmpeg": "",
    }


def normalize_dir(p):
    """统一成规范绝对路径；顺手把 C://音乐 这种双重斜杠修正掉。"""
    if not p:
        return ""
    try:
        return str(Path(os.path.normpath(str(p).strip())))
    except Exception:
        return str(p).strip()


def load_config():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = default_config()
    if CONFIG_FILE.is_file():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    cfg["out"] = normalize_dir(cfg.get("out") or default_config()["out"])

    # 导入目录：去重、去掉不存在/自身输出目录/程序目录
    o = Path(cfg["out"]).resolve() if cfg["out"] else None
    dirs, seen = [], set()
    for d in cfg.get("importDirs") or []:
        nd = normalize_dir(d)
        if not nd or not Path(nd).is_dir():
            continue
        rp = Path(nd).resolve()
        if o and (rp == o or rp in o.parents or o in rp.parents):
            continue
        if rp == APP_DIR or APP_DIR in rp.parents:
            continue
        k = str(rp).lower()
        if k in seen:
            continue
        seen.add(k)
        dirs.append(nd)
    cfg["importDirs"] = dirs

    # 散装文件：同样去重并剔除落在输出目录里的
    extras, eseen = [], set()
    for x in cfg.get("extraFiles") or []:
        nx = normalize_dir(x)
        if not nx or not Path(nx).is_file() or Path(nx).suffix.lower() not in AUDIO_EXTS:
            continue
        rp = Path(nx).resolve()
        if o and (rp == o or o in rp.parents):
            continue
        k = str(rp).lower()
        if k in eseen:
            continue
        eseen.add(k)
        extras.append(nx)
    cfg["extraFiles"] = extras
    return cfg


def save_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- 路径

def game_cache_dir():
    base = Path(os.environ.get("APPDATA") or Path.home())
    return base / "miHoYo" / "Olivia-steam" / "cache" / "studio" / "video"


def image_cache_dir():
    base = Path(os.environ.get("APPDATA") or Path.home())
    return base / "miHoYo" / "Olivia-steam" / "cache" / "studio" / "image"


def out_dir(cfg=None):
    cfg = cfg or load_config()
    return Path(cfg["out"])


def dir_m4a(cfg=None):
    return out_dir(cfg) / "无损 m4a"


def dir_mp3(cfg=None):
    return out_dir(cfg) / "mp3 256k"


def dir_cover(cfg=None):
    return out_dir(cfg) / "封面"


# ---------------------------------------------------------------- 曲目表

_catalog_cache = {}


def load_catalog():
    if "data" in _catalog_cache:
        return _catalog_cache["data"]
    data = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    songs = data["songs"]
    for i, s in enumerate(songs, start=1):
        s["idx"] = i
    _catalog_cache["data"] = data
    return data


def track_stem(song):
    return safe(f"{song['idx']:02d} {song['artist']} - {song['title']}")


# ---------------------------------------------------------------- 探测

def _run(cmd, timeout=1800):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe(path, ffprobe):
    """返回 (时长秒, 码率, 编码名)。没有 ffprobe 就返回空。"""
    if not ffprobe:
        return 0.0, 0, ""
    try:
        out = _run([ffprobe, "-v", "error", "-select_streams", "a:0",
                    "-show_entries", "stream=codec_name,bit_rate:format=duration",
                    "-of", "json", str(path)], timeout=30).stdout
        info = json.loads(out or "{}")
        dur = float(info.get("format", {}).get("duration") or 0)
        st = (info.get("streams") or [{}])[0]
        return dur, int(st.get("bit_rate") or 0), str(st.get("codec_name") or "")
    except Exception:
        return 0.0, 0, ""


def probe_tags(path, ffprobe):
    if not ffprobe:
        return {}
    try:
        out = _run([ffprobe, "-v", "error", "-show_entries", "format_tags",
                    "-of", "json", str(path)], timeout=30).stdout
        raw = json.loads(out or "{}").get("format", {}).get("tags", {}) or {}
        return {str(k).lower(): str(v) for k, v in raw.items()}
    except Exception:
        return {}


def _cache_load():
    try:
        return json.loads(PROBE_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _cache_save(c):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PROBE_CACHE.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def probe_meta(path, ffprobe, use_cache=True):
    """带缓存的探测，返回 (dur, br, codec, tags)。"""
    cache = _cache_load() if use_cache else {}
    try:
        st = Path(path).stat()
        key = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        key = ""
    hit = cache.get(str(path))
    if use_cache and hit and hit.get("k") == key:
        return hit["d"], hit.get("b", 0), hit.get("c", ""), hit.get("t", {})
    dur, br, codec = probe(path, ffprobe)
    tags = probe_tags(path, ffprobe)
    if use_cache:
        cache[str(path)] = {"k": key, "d": dur, "b": br, "c": codec, "t": tags}
        _cache_save(cache)
    return dur, br, codec, tags


# ---------------------------------------------------------------- 扫描

def scan_videos(video_dir=None):
    """返回 {key: (tod, Path, 版本数)}"""
    vd = Path(video_dir) if video_dir else game_cache_dir()
    if not vd.is_dir():
        return {}
    takes = {}
    for p in sorted(vd.rglob("*.mp4")):
        m = VIDEO_RE.match(p.stem)
        if m:
            takes.setdefault(m.group("key"), []).append((m.group("tod"), p))
    picked = {}
    for key, items in takes.items():
        by_tod = {t: p for t, p in items}
        for tod in PREFERRED_TOD:
            if tod in by_tod:
                picked[key] = (tod, by_tod[tod], len(items))
                break
        else:
            tod, p = sorted(items)[0]
            picked[key] = (tod, p, len(items))
    return picked


def scan_audio(dirs, exclude=()):
    """递归找音乐文件。"""
    skip = [Path(p).resolve() for p in exclude if p]
    found, seen = [], set()
    for d in dirs:
        base = Path(d)
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            try:
                if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
                    continue
            except OSError:
                continue
            rp = p.resolve()
            if rp in seen:
                continue
            if any(rp == s or s in rp.parents for s in skip):
                continue
            if any(part.startswith(".") for part in p.relative_to(base).parts):
                continue
            seen.add(rp)
            found.append(p)
    return found


SKIP_DIRS = {
    "windows", "program files", "program files (x86)", "programdata", "appdata",
    "system volume information", "$recycle.bin", "recovery", "perflogs",
    "node_modules", ".git", "packages", "windowsapps", "steamapps",
    "__pycache__", ".cache", "onedrivetemp", "temp", "tmp", "cache",
}


def autoscan(progress=None, mode="quick", max_depth=7, cap=20000,
             exclude=(), ffprobe=None, min_sec=20):
    """自动扫盘找音乐。quick=常见目录，full=所有固定磁盘（带深度与数量上限）。

    min_sec：丢给 ffprobe 过滤掉短音效（提示音、闹钟、游戏音效），只留下像"歌"的东西。
    """
    home = Path.home()
    skip = [Path(p).resolve() for p in exclude if p]

    def is_excluded(p: Path):
        try:
            rp = p.resolve()
        except OSError:
            return True
        return any(rp == s or s in rp.parents for s in skip)

    quick_dirs = [
        home / "Music", home / "Desktop", home / "Downloads", home / "Documents",
        home / "Videos", home / "OneDrive", home / "WPS Cloud",
        Path("C:/音乐"), Path("C:/音樂"), Path("D:/音乐"), Path("D:/Music"),
        Path("D:/KuGou"), Path("D:/LXF/Music"), Path("C:/Users/Public/Music"),
    ]

    if mode == "quick":
        dirs = [d for d in quick_dirs if d.is_dir() and not is_excluded(d)]
        if progress:
            progress("快速扫描常见音乐目录…", 8)
        found = scan_audio(dirs, exclude=skip)
    else:
        roots = []
        if os.name == "nt":
            import string
            for ch in string.ascii_uppercase:
                d = Path(f"{ch}:/")
                try:
                    if d.is_dir():
                        roots.append(d)
                except OSError:
                    pass
        else:
            roots = [home]

        found, seen, scanned = [], set(), 0
        for root in roots:
            base_depth = len(root.parts)
            for cur, dirnames, filenames in os.walk(root, onerror=lambda e: None):
                depth = len(Path(cur).parts) - base_depth
                dirnames[:] = [d for d in dirnames
                               if d.lower() not in SKIP_DIRS and not d.startswith(".")]
                if depth >= max_depth:
                    dirnames[:] = []
                for fn in filenames:
                    scanned += 1
                    if Path(fn).suffix.lower() in AUDIO_EXTS:
                        p = Path(cur) / fn
                        if not is_excluded(p):
                            try:
                                rp = p.resolve()
                            except OSError:
                                continue
                            if rp not in seen:
                                seen.add(rp)
                                found.append(p)
                                if progress and len(found) % 20 == 0:
                                    progress(f"已找到 {len(found)} 首…", min(90, 10 + len(found) // 8))
                    if scanned >= cap:
                        break
                if scanned >= cap:
                    break
            if scanned >= cap:
                break
        if progress:
            progress(f"扫到 {len(found)} 个音频文件，正在剔除短音效…", 92)

    # 用时长筛一遍：提示音/闹钟/音效通常几秒钟，歌不会
    if ffprobe and found:
        kept = []
        for i, p in enumerate(found, start=1):
            if progress and i % 25 == 0:
                progress(f"校验时长 {i}/{len(found)}…", 92 + min(6, i * 6 // max(len(found), 1)))
            dur = probe(p, ffprobe)[0]
            if dur == 0 or dur >= min_sec:
                kept.append(p)
        found = kept

    if progress:
        progress(f"扫描完成，可用音频 {len(found)} 个", 100)
    return found


# ---------------------------------------------------------------- 提取

def extract(ffmpeg, video, cover, out_m4a, out_mp3, tags):
    if not ffmpeg:
        raise RuntimeError("缺少 ffmpeg")
    meta = []
    for k, v in tags.items():
        meta += ["-metadata", f"{k}={v}"]
    has_cover = cover is not None and Path(cover).is_file()

    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(video)]
    if has_cover:
        cmd += ["-i", str(cover)]
    cmd += ["-map", "0:a:0"]
    if has_cover:
        cmd += ["-map", "1:v:0", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic"]
    cmd += ["-c:a", "copy", "-movflags", "+faststart", *meta, str(out_m4a)]
    r = _run(cmd)
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-1:]
        raise RuntimeError(tail[0] if tail else "提取失败")

    if out_mp3 is not None:
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(video)]
        if has_cover:
            cmd += ["-i", str(cover)]
        cmd += ["-map", "0:a:0"]
        if has_cover:
            cmd += ["-map", "1:v:0", "-c:v", "copy"]
        cmd += ["-c:a", "libmp3lame", "-b:a", "256k", "-id3v2_version", "3"]
        if has_cover:
            cmd += ["-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
        cmd += [*meta, str(out_mp3)]
        r = _run(cmd)
        if r.returncode != 0:
            tail = (r.stderr or "").strip().splitlines()[-1:]
            raise RuntimeError(tail[0] if tail else "转 mp3 失败")


def extract_song(song, video, cfg, ffmpeg, want_mp3=True):
    o = out_dir(cfg)
    c = dir_cover(cfg)
    c.mkdir(parents=True, exist_ok=True)
    dir_m4a(cfg).mkdir(parents=True, exist_ok=True)
    if want_mp3:
        dir_mp3(cfg).mkdir(parents=True, exist_ok=True)

    cover_path = None
    cov = song.get("cover")
    if cov and (image_cache_dir() / cov).is_file():
        cover_path = c / f"{safe(song['artist'] + ' - ' + song['title'])}.jpg"
        shutil.copy2(image_cache_dir() / cov, cover_path)

    stem = track_stem(song)
    m4a = dir_m4a(cfg) / f"{stem}.m4a"
    mp3 = dir_mp3(cfg) / f"{stem}.mp3" if want_mp3 else None
    tags = {
        "title": song["title"],
        "artist": f"{song['artist']} / 林离 Olivia Lin",
        "album": "林离曲库 · BSide 离线版导出",
        "album_artist": "Olivia Lin 林离",
        "track": str(song["idx"]),
        "genre": song["cat"],
        "comment": "Extracted from BSide: Olivia Lin local cache. 仅供个人离线欣赏。",
    }
    extract(ffmpeg, video, cover_path, m4a, mp3, tags)
    return m4a, mp3


# ---------------------------------------------------------------- 组装状态

def build_state(cfg=None, with_local=True):
    """给界面用的完整数据。"""
    cfg = cfg or load_config()
    ffmpeg = find_ffmpeg(cfg)
    ffprobe = ffprobe_of(ffmpeg)
    o = out_dir(cfg)
    videos = scan_videos()
    catalog = load_catalog()

    tracks = []
    ready = extractable = 0
    for song in catalog["songs"]:
        key = song["key"]
        stem = track_stem(song)
        m4a = dir_m4a(cfg) / f"{stem}.m4a"
        mp3 = dir_mp3(cfg) / f"{stem}.mp3"
        cvg_name = song.get("cover")
        cvg = None
        if cvg_name and (image_cache_dir() / cvg_name).is_file():
            cvg = image_cache_dir() / cvg_name
        elif (dir_cover(cfg) / f"{safe(song['artist'] + ' - ' + song['title'])}.jpg").is_file():
            cvg = dir_cover(cfg) / f"{safe(song['artist'] + ' - ' + song['title'])}.jpg"

        item = {
            "id": key, "kind": "olivia", "idx": song["idx"],
            "title": song["title"], "artist": song["artist"], "cat": song["cat"],
            "dur": 0, "codec": "aac", "cover": str(cvg) if cvg else None,
            "file": None, "alt": None,
        }
        if m4a.is_file():
            item["state"] = "ready"
            item["file"] = str(m4a)
            if mp3.is_file():
                item["alt"] = str(mp3)
            item["dur"] = probe(m4a, ffprobe)[0] or 0
            ready += 1
        elif key in videos:
            item["state"] = "extractable"
            item["video"] = str(videos[key][1])
            item["takes"] = videos[key][2]
            extractable += 1
        else:
            item["state"] = "missing"
        tracks.append(item)

    local = []
    if with_local and (cfg["importDirs"] or cfg.get("extraFiles")):
        files = scan_audio(cfg["importDirs"], exclude=[o, APP_DIR])
        seen = {str(f.resolve()) for f in files}
        for x in cfg.get("extraFiles") or []:
            p = Path(x)
            try:
                if p.is_file() and p.suffix.lower() in AUDIO_EXTS and str(p.resolve()) not in seen:
                    seen.add(str(p.resolve()))
                    files.append(p)
            except OSError:
                continue
        for f in files:
            dur, _br, codec, tags = probe_meta(f, ffprobe)
            title = (tags.get("title") or tags.get("tit2") or f.stem).strip()
            artist = (tags.get("artist") or tags.get("album_artist") or "未知艺术家").strip()
            local.append({
                "id": "LOCAL::" + str(f), "kind": "local", "idx": 0,
                "title": title, "artist": artist, "cat": "我的音乐",
                "dur": dur or 0, "codec": codec, "cover": None,
                "file": str(f), "alt": None, "state": "ready",
                "folder": str(f.parent),
            })

    return {
        "config": {
            "out": str(o),
            "importDirs": cfg["importDirs"],
            "cacheDir": str(game_cache_dir()),
            "cacheFound": game_cache_dir().is_dir(),
            "videoCount": len(videos),
            "ffmpeg": bool(ffmpeg),
            "ffmpegPath": ffmpeg or "",
            "appDir": str(APP_DIR),
        },
        "stats": {
            "ready": ready,
            "extractable": extractable,
            "missing": len(catalog["songs"]) - ready - extractable,
            "total": len(catalog["songs"]),
            "local": len(local),
            "seconds": int(sum(t["dur"] for t in tracks if t["state"] == "ready")),
        },
        "tracks": tracks + local,
    }

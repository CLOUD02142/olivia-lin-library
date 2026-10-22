#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
林离曲库 / BSide: Olivia Lin 离线版音轨提取 + 本地音乐库

两件事：
  1. 提取：把游戏缓存里已经下好的「演奏视频」拆出音轨，整理成音乐文件
     （无损 m4a + mp3 + 封面 + 标签）。
  2. 导入：把你自己电脑上的音乐目录登记进同一个曲库页面，与林离的曲目分区显示。
     默认只登记路径、不复制文件（省空间）；加 --copy-import 才复制进来。

用法：
  python export.py                          # 提取 + 导入（自动探测音乐目录）
  python export.py --import-dir "C:\\音乐"   # 指定要导入的音乐目录
  python export.py --no-import              # 只提取，不导入
  python export.py --copy-import            # 把导入的音乐复制进曲库目录
  python export.py --src "D:\\备份\\video"   # 换一个演奏视频来源目录
  python export.py --out "D:\\Music\\林离"   # 换输出目录
  python export.py --keep-video             # 顺便复制原版 mp4
  python export.py --no-mp3                 # 只要无损 m4a

说明：
  同一首曲子的 12:00 / 17:30 / 20:00 三个版本是同一次演奏（响度一致、只是打光不同），
  默认每曲只导一份，优先取 12:00。

关于「下载」：原版靠服务器下发的短期签名链接取视频，2026-08-31 关服后签名接口关闭，
CDN 对无签名请求一律 401，剩余曲目无法再取回。本工具只能处理已存在于本地的文件。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
CATALOG_FILE = HERE / "曲目表.json"
TEMPLATE_FILE = HERE / "曲库模板.html"

DEFAULT_SRC = Path(os.environ.get("APPDATA", "")) / "miHoYo" / "Olivia-steam" / "cache" / "studio" / "video"
DEFAULT_IMAGE_CACHE = Path(os.environ.get("APPDATA", "")) / "miHoYo" / "Olivia-steam" / "cache" / "studio" / "image"
DEFAULT_OUT = Path(os.environ.get("USERPROFILE", "")) / "Music" / "林离曲库"

IMPORT_CANDIDATES = [
    Path("C:/音乐"),
    Path("C:/音樂"),
    Path("D:/音乐"),
    Path(os.environ.get("USERPROFILE", "")) / "Music",
    Path(os.environ.get("USERPROFILE", "")) / "Music" / "我的音乐",
    Path("D:/LXF/Music"),
]

VIDEO_RE = re.compile(r"^(?P<key>.+?)_TOD(?P<tod>\d{3,4})_NI(?:_L)?$", re.I)
PREFERRED_TOD = ["1200", "1730", "2000"]
ILLEGAL = re.compile(r'[\\/:*?"<>|]')

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus", ".ape", ".wma"}
BROWSER_OK = {"mp3", "aac", "vorbis", "opus", "flac", "alac", "pcm_s16le", "pcm_s24le", "mp4a"}

FFMPEG_CANDIDATES = [
    os.environ.get("FFMPEG", ""),
    "ffmpeg",
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    "/usr/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
]


def find_ffmpeg():
    for cand in FFMPEG_CANDIDATES:
        if not cand:
            continue
        if Path(cand).is_file():
            return str(Path(cand))
        found = shutil.which(cand)
        if found:
            return found
    sys.exit("找不到 ffmpeg。装一个，或设置环境变量 FFMPEG 指向 ffmpeg.exe。")


def safe(name: str) -> str:
    return ILLEGAL.sub("_", name).strip().rstrip(".")


def load_catalog():
    with open(CATALOG_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    by_key, order = {}, {}
    for i, song in enumerate(data["songs"]):
        by_key[song["key"]] = song
        order[song["key"]] = i
    return data, by_key, order


def fallback_title(key: str) -> str:
    s = re.sub(r"^(Solo|PlaySing)_", "", key)
    s = s.replace("_Ziyun_original", "").replace("_original", "")
    return s.replace("_", " ")


def default_import_dir():
    for p in IMPORT_CANDIDATES:
        try:
            if p.is_dir() and any(f.suffix.lower() in AUDIO_EXTS for f in p.iterdir() if f.is_file()):
                return p
        except (OSError, PermissionError):
            continue
    return None


# ---------------------------------------------------------------- 扫描

def scan_videos(src: Path, order):
    if not src.is_dir():
        return {}
    takes = {}
    for p in sorted(src.rglob("*.mp4")):
        m = VIDEO_RE.match(p.stem)
        if not m:
            continue
        takes.setdefault(m.group("key"), []).append((m.group("tod"), p))

    picked = {}
    for key, items in takes.items():
        by_tod = {tod: p for tod, p in items}
        for tod in PREFERRED_TOD:
            if tod in by_tod:
                picked[key] = (tod, by_tod[tod], len(items))
                break
        else:
            tod, p = sorted(items)[0]
            picked[key] = (tod, p, len(items))
    return dict(sorted(picked.items(), key=lambda kv: order.get(kv[0], 9999)))


def scan_audio(import_dir: Path, exclude):
    """递归找音乐文件，跳过输出目录、程序目录、隐藏目录。"""
    if import_dir is None or not import_dir.is_dir():
        return []
    skip = [Path(p).resolve() for p in exclude if p]
    out = []
    for p in sorted(import_dir.rglob("*")):
        try:
            if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
                continue
        except OSError:
            continue
        rp = p.resolve()
        if any(rp == s or s in rp.parents for s in skip):
            continue
        if any(part.startswith(".") for part in p.relative_to(import_dir).parts):
            continue
        out.append(p)
    return out


# ---------------------------------------------------------------- 探测

def probe(ffprobe: str, path: Path):
    dur, br, codec = 0.0, 0, ""
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name,bit_rate:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        ).stdout
        info = json.loads(out or "{}")
        dur = float(info.get("format", {}).get("duration") or 0)
        st = (info.get("streams") or [{}])[0]
        br = int(st.get("bit_rate") or 0)
        codec = str(st.get("codec_name") or "")
    except Exception:
        pass
    return dur, br, codec


def probe_tags(ffprobe: str, path: Path):
    tags = {}
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format_tags",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        ).stdout
        raw = json.loads(out or "{}").get("format", {}).get("tags", {}) or {}
        tags = {str(k).lower(): str(v) for k, v in raw.items()}
    except Exception:
        pass
    return tags


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-1:]
        raise RuntimeError(tail[0] if tail else "ffmpeg 执行失败")


def extract(ffmpeg, src_video: Path, cover, out_m4a: Path, out_mp3, tags):
    meta = []
    for k, v in tags.items():
        meta += ["-metadata", f"{k}={v}"]
    has_cover = cover is not None and Path(cover).is_file()

    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src_video)]
    if has_cover:
        cmd += ["-i", str(cover)]
    cmd += ["-map", "0:a:0"]
    if has_cover:
        cmd += ["-map", "1:v:0", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic"]
    cmd += ["-c:a", "copy", "-movflags", "+faststart", *meta, str(out_m4a)]
    run(cmd)

    if out_mp3 is not None:
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src_video)]
        if has_cover:
            cmd += ["-i", str(cover)]
        cmd += ["-map", "0:a:0"]
        if has_cover:
            cmd += ["-map", "1:v:0", "-c:v", "copy"]
        cmd += ["-c:a", "libmp3lame", "-b:a", "256k", "-id3v2_version", "3"]
        if has_cover:
            cmd += ["-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
        cmd += [*meta, str(out_mp3)]
        run(cmd)


def fmt_dur(sec):
    if not sec:
        return "—"
    sec = int(round(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(DEFAULT_SRC))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--image-cache", default=str(DEFAULT_IMAGE_CACHE))
    ap.add_argument("--import-dir", default=None, help="要导入的本地音乐目录，默认自动探测")
    ap.add_argument("--no-import", action="store_true", help="不导入本地音乐")
    ap.add_argument("--copy-import", action="store_true", help="把导入的音乐复制进曲库目录")
    ap.add_argument("--keep-video", action="store_true")
    ap.add_argument("--no-mp3", action="store_true")
    args = ap.parse_args()

    ffmpeg = find_ffmpeg()
    ffprobe = str(Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe"))
    if not Path(ffprobe).is_file():
        ffprobe = shutil.which("ffprobe") or ffprobe

    catalog, by_key, order = load_catalog()
    src, out = Path(args.src), Path(args.out)
    image_cache = Path(args.image_cache)
    out.mkdir(parents=True, exist_ok=True)

    dir_lossless = out / "无损 m4a"
    dir_mp3 = out / "mp3 256k"
    dir_cover = out / "封面"
    dir_video = out / "原版视频"
    dir_import = out / "我的音乐"

    # ---- 曲目池：先铺满整张曲目表（含没拿到的），再往里填 ----
    rows = []
    for i, song in enumerate(catalog["songs"], start=1):
        rows.append({
            "i": i, "key": song["key"], "title": song["title"], "artist": song["artist"],
            "cat": song["cat"], "dur": None, "m4a": None, "mp3": None, "cover": None,
            "abs": None, "codec": None,
        })
    index_of = {r["key"]: r for r in rows}

    picked = scan_videos(src, order)
    for key in picked:
        if key not in index_of:
            r = {"i": len(rows) + 1, "key": key, "title": fallback_title(key),
                 "artist": "未收录", "cat": "未知", "dur": None, "m4a": None,
                 "mp3": None, "cover": None, "abs": None, "codec": None}
            rows.append(r)
            index_of[key] = r

    # ---- 提取演奏音轨 ----
    if picked:
        print(f"演奏视频：{src}")
        dir_lossless.mkdir(parents=True, exist_ok=True)
        dir_cover.mkdir(parents=True, exist_ok=True)
        if not args.no_mp3:
            dir_mp3.mkdir(parents=True, exist_ok=True)
        if args.keep_video:
            dir_video.mkdir(parents=True, exist_ok=True)

    done, total_sec = 0, 0.0
    if not picked:
        print(f"（{src} 下没有演奏视频，跳过提取）")

    for key, (tod, video, variants) in picked.items():
        song = by_key.get(key)
        row = index_of[key]
        title, artist = row["title"], row["artist"]
        dur, br, _ = probe(ffprobe, video)
        total_sec += dur

        cover_path = None
        cov_name = (song or {}).get("cover")
        if cov_name and (image_cache / cov_name).is_file():
            cover_path = dir_cover / f"{safe(artist + ' - ' + title)}.jpg"
            shutil.copy2(image_cache / cov_name, cover_path)
            row["cover"] = cover_path.relative_to(out).as_posix()

        stem = safe(f"{row['i']:02d} {artist} - {title}")
        out_m4a = dir_lossless / f"{stem}.m4a"
        out_mp3 = None if args.no_mp3 else dir_mp3 / f"{stem}.mp3"
        tags = {
            "title": title,
            "artist": f"{artist} / 林离 Olivia Lin",
            "album": "林离曲库 · BSide 离线版导出",
            "album_artist": "Olivia Lin 林离",
            "track": str(row["i"]),
            "genre": row["cat"],
            "comment": "Extracted from BSide: Olivia Lin local cache. 仅供个人离线欣赏。",
        }
        print(f"[{row['i']:02d}] {title} · {artist}  {fmt_dur(dur)}  ~{br // 1000}kbps  ({variants} 个时段版本 → 取 {tod})")
        try:
            extract(ffmpeg, video, cover_path, out_m4a, out_mp3, tags)
        except Exception as e:
            print(f"     × 提取失败：{e}")
            continue
        row.update({"dur": dur, "m4a": out_m4a.relative_to(out).as_posix(),
                    "mp3": (out_mp3.relative_to(out).as_posix() if out_mp3 else None),
                    "codec": "aac"})
        if args.keep_video:
            shutil.copy2(video, dir_video / f"{stem}.mp4")
        done += 1

    # ---- 导入本地音乐 ----
    imported = 0
    import_dir = None
    if not args.no_import:
        import_dir = Path(args.import_dir) if args.import_dir else default_import_dir()
    if import_dir:
        audio_files = scan_audio(import_dir, [out, HERE])
        if audio_files:
            print(f"\n本地音乐：{import_dir}（{len(audio_files)} 个文件）")
            if args.copy_import:
                dir_import.mkdir(parents=True, exist_ok=True)

            cache_file = out / ".probe-cache.json"
            cache = {}
            if cache_file.is_file():
                try:
                    cache = json.loads(cache_file.read_text(encoding="utf-8"))
                except Exception:
                    cache = {}

            start_i = len(rows)
            for n, f in enumerate(audio_files, start=1):
                try:
                    stt = f.stat()
                except OSError:
                    continue
                ck = f"{stt.st_mtime_ns}:{stt.st_size}"
                hit = cache.get(str(f))
                if hit and hit.get("ck") == ck:
                    dur, codec, tags = hit["dur"], hit["codec"], hit["tags"]
                else:
                    dur, _br, codec = probe(ffprobe, f)
                    tags = probe_tags(ffprobe, f)
                    cache[str(f)] = {"ck": ck, "dur": dur, "codec": codec, "tags": tags}

                title = (tags.get("title") or tags.get("tit2") or f.stem).strip()
                artist = (tags.get("artist") or tags.get("album_artist")
                          or (f.parent.name if f.parent != import_dir else "") or "未知艺术家").strip()

                rel = None
                if args.copy_import:
                    dst = dir_import / safe(f"{start_i + n:03d} {artist} - {title}{f.suffix.lower()}")
                    if not dst.exists():
                        shutil.copy2(f, dst)
                    rel = dst.relative_to(out).as_posix()

                rows.append({
                    "i": start_i + n,
                    "key": f"LOCAL::{f}",
                    "title": title,
                    "artist": artist,
                    "cat": "我的音乐",
                    "dur": dur,
                    "m4a": rel,
                    "mp3": None,
                    "cover": None,
                    "abs": None if rel else str(f),
                    "codec": codec,
                })
                imported += 1

            try:
                cache_file.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

            if args.copy_import:
                # 复制模式下只留曲库目录里的相对路径
                pass
        else:
            print(f"\n本地音乐：{import_dir} 里没找到音频文件")
    elif not args.no_import:
        print("\n本地音乐：没找到可导入的目录（用 --import-dir 指定）")

    lib = {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": str(src),
        "importDir": str(import_dir) if import_dir else None,
        "output": str(out),
        "stats": {
            "total": len([r for r in rows if r["cat"] != "我的音乐"]),
            "local": done,
            "imported": imported,
            "seconds": int(total_sec),
        },
        "tracks": rows,
    }

    (out / "library.json").write_text(json.dumps(lib, ensure_ascii=False, indent=1), encoding="utf-8")

    if TEMPLATE_FILE.is_file():
        html = TEMPLATE_FILE.read_text(encoding="utf-8").replace(
            "__LIBRARY_DATA__", json.dumps(lib, ensure_ascii=False)
        )
        (out / "曲库.html").write_text(html, encoding="utf-8")

    print(f"\n完成：林离 {done} 首 / 导入 {imported} 首，演奏总时长 {fmt_dur(total_sec)}")
    print(f"输出目录：{out}")
    print(f"打开这个文件听：{out / '曲库.html'}")


if __name__ == "__main__":
    main()

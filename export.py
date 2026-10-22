#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export.py —— 命令行版（不开界面时用）

想用界面就双击 启动.bat 或者 python app.py。

用法：
  python export.py                        # 把缓存里能导的曲目全部导出
  python export.py --no-mp3               # 只出无损 m4a
  python export.py --import-dir "C:\\音乐"  # 追加一个本地音乐目录
  python export.py --auto-scan quick      # 顺便扫一遍常见音乐目录
  python export.py --out "D:\\Music\\林离"  # 换输出目录
  python export.py --status               # 只看当前状态，不动手
"""

import argparse
import sys
from pathlib import Path

import core


def human(sec):
    sec = int(sec or 0)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="输出目录")
    ap.add_argument("--import-dir", action="append", default=[], help="追加本地音乐目录（可多次）")
    ap.add_argument("--auto-scan", choices=["quick", "full"], help="先扫盘再导入")
    ap.add_argument("--no-mp3", action="store_true", help="只出无损 m4a")
    ap.add_argument("--status", action="store_true", help="只显示状态")
    args = ap.parse_args()

    cfg = core.load_config()
    if args.out:
        cfg["out"] = args.out
        Path(args.out).mkdir(parents=True, exist_ok=True)
    if args.import_dir:
        for d in args.import_dir:
            if Path(d).is_dir() and d not in cfg["importDirs"]:
                cfg["importDirs"].append(d)
                print(f"已加入音乐目录：{d}")
    core.save_config(cfg)

    state = core.build_state(cfg)
    st, c = state["stats"], state["config"]
    print("=" * 56)
    print(f"缓存目录：{c['cacheDir']}  {'✓' if c['cacheFound'] else '（不存在）'}")
    print(f"输出目录：{c['out']}")
    print(f"ffmpeg  ：{c['ffmpegPath'] or '未找到'}")
    print(f"曲目    ：已就绪 {st['ready']} / 可导出 {st['extractable']} / 未获取 {st['missing']}")
    print(f"本地音乐：{st['local']} 首")
    print("=" * 56)

    if args.status:
        return

    if args.auto_scan:
        print("正在扫盘…")
        files = core.autoscan(mode=args.auto_scan,
                              progress=lambda m, p: None)
        dirs = sorted({str(f.parent) for f in files})
        added = 0
        for d in dirs:
            if d not in cfg["importDirs"]:
                cfg["importDirs"].append(d)
                added += 1
        core.save_config(cfg)
        print(f"扫到 {len(files)} 个音频文件，新增 {added} 个目录")

    if st["extractable"]:
        ffmpeg = core.find_ffmpeg(cfg)
        if not ffmpeg:
            print("✗ 缺少 ffmpeg，先装一个再导出。")
            return
        videos = core.scan_videos()
        catalog = core.load_catalog()
        todo = [s for s in catalog["songs"]
                if s["key"] in videos and not (core.dir_m4a(cfg) / f"{core.track_stem(s)}.m4a").is_file()]
        print(f"开始导出 {len(todo)} 首…")
        for i, song in enumerate(todo, start=1):
            print(f"  [{i}/{len(todo)}] {song['title']} · {song['artist']}", end=" ")
            try:
                core.extract_song(song, videos[song["key"]][1], cfg, ffmpeg,
                                  want_mp3=not args.no_mp3)
                print("✓")
            except Exception as e:
                print(f"✗ {e}")
    else:
        print("没有可导出的曲目（缓存里没有演奏视频）。")

    state = core.build_state(cfg)
    print(f"\n完成。已就绪 {state['stats']['ready']} 首，"
          f"演奏时长 {human(state['stats']['seconds'])}")
    print("打开界面看：双击 启动.bat")


if __name__ == "__main__":
    main()

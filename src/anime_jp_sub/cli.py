# -*- coding: utf-8 -*-
"""命令行入口。

子命令：
  process   处理：没有日语字幕轨的集数做 听写 → 注音 → 内封（默认子命令）
  scan      只扫描报告，不改任何文件
  review    给已经处理好的集数生成"评审页"（边看边标哪句断错了 / 哪句漏了）
  score     拿人工评审的标记给切分算法打分（开发/调参用，见 dev 分支的 qa/score）
  doctor    环境自检（外部程序 / python 包 / 模型目录）
  version   打印版本

向后兼容：不带子命令时按 process 处理，所以老的用法
`anime_jp_sub.py "D:/Anime/2026.7"` 照旧能用。
"""

import argparse
import sys
from pathlib import Path

from . import __version__, common

SUBCOMMANDS = ("process", "scan", "review", "score", "doctor", "version")


def build_parser():
    ap = argparse.ArgumentParser(
        prog="anime-jp-sub",
        description="给日语动画 mkv 自动生成带平假名注音的日语字幕并内封（不重编码音视频）")
    ap.add_argument("--version", action="version", version=f"anime-jp-sub {__version__}")
    sub = ap.add_subparsers(dest="command")

    p = sub.add_parser("process", help="处理没有日语字幕的集数（默认）")
    _add_target(p)
    p.add_argument("--keep-srt", action="store_true",
                   help="保留生成的字幕文件供调试（默认用完即弃）")
    p.add_argument("--keep-dump", dest="keep_dump", action="store_true",
                   help="保留这集的听写+VAD 结果（.jp.dump.json），给 qa/score 打分用")
    p.add_argument("--no-furigana", dest="furigana", action="store_false",
                   help="不生成平假名注音，退回普通 SRT 字幕轨")
    p.add_argument("--all", action="store_true",
                   help="（兼容保留，无额外效果）时间闸已移除，全量扫描是默认行为")
    p.add_argument("--download-tools", dest="download_tools", action="store_true",
                   help="缺 ffmpeg / MKVToolNix 时直接下到项目 tools/（约 200MB，不装进系统）")
    p.set_defaults(furigana=True)

    s = sub.add_parser("scan", help="只扫描并报告，不改动文件")
    _add_target(s)

    d = sub.add_parser("doctor", help="环境自检")
    d.add_argument("--download-tools", dest="download_tools", action="store_true",
                   help="顺手把缺的 ffmpeg / MKVToolNix 下到项目 tools/")

    r = sub.add_parser("review", help="生成评审页：边看动画边标「哪句断错了 / 哪句漏了」")
    _add_target(r)
    r.add_argument("--force", action="store_true",
                   help="已有评审页也重新生成（默认跳过）")

    sc = sub.add_parser("score", help="拿评审标记给切分算法打分（开发/调参用）")
    sc.add_argument("marks", help="评审页导出的标记文本")
    sc.add_argument("--baseline", required=True,
                    help="人当时审的那份产物（.ass/.srt）——标记的行号属于它")
    sc.add_argument("--dump", required=True,
                    help="那集的听写结果（.jp.dump.json，来自 process --keep-dump）")
    sc.add_argument("--tolerance", type=int, default=1,
                    help="允许差几个词缝算命中（默认 1，别改成 0 除非你知道为什么）")
    sc.add_argument("-v", "--verbose", action="store_true",
                    help="逐条列出没通过的（带时间码，方便回听）")
    sub.add_parser("version", help="打印版本")
    return ap


def _add_target(p):
    p.add_argument("target", nargs="?", default=None,
                   help="季度文件夹 / 番剧文件夹 / 单个 mkv；缺省用当前目录")


def main(argv=None):
    common.setup_console()      # 中文控制台(GBK)下防止生僻符号把命令打断
    common.remember_project_path()   # 给"复制到别处双击"的 .bat 留个路标
    argv = list(sys.argv[1:] if argv is None else argv)
    # 兼容老用法：第一个参数不是子命令时，当成 process 的目标
    if argv and argv[0] not in SUBCOMMANDS and not argv[0].startswith("-"):
        argv = ["process"] + argv
    elif not argv:
        argv = ["process"]
    elif argv[0].startswith("--") and argv[0] not in ("--version", "-h", "--help"):
        argv = ["process"] + argv

    args = build_parser().parse_args(argv)
    cmd = args.command or "process"

    if cmd == "version":
        print(f"anime-jp-sub {__version__}")
        return 0
    if cmd == "doctor":
        return common.doctor(download_tools=getattr(args, "download_tools", False))
    if cmd == "review":
        from .qa import review
        return review.generate(Path(args.target) if args.target else Path.cwd(),
                               overwrite=args.force)
    if cmd == "score":
        from .qa.score.run import run as score_run
        return score_run(args.marks, args.baseline, args.dump,
                         tolerance=args.tolerance,
                         verbose=args.verbose)

    from . import pipeline
    target = Path(args.target) if args.target else Path.cwd()
    if not target.exists():
        print(f"找不到目标：{target}")
        return 2
    if cmd == "scan":
        return pipeline.scan(target)
    return pipeline.process(target, keep_srt=args.keep_srt, use_furigana=args.furigana,
                            auto_download_tools=args.download_tools,
                            keep_dump=args.keep_dump)


if __name__ == "__main__":
    sys.exit(main())

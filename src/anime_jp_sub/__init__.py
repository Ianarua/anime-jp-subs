# -*- coding: utf-8 -*-
"""anime-jp-sub —— 给日语动画 mkv 自动生成带平假名注音的日语字幕并内封。

模块划分：
  cli.py       命令行入口（子命令 process / scan / doctor / version）
  pipeline.py  五步流水线（扫描 → 抽音频 → 听写 → 断句对齐 → 内封 → 自检 → 替换）
  furigana.py  注音（读音、送假名对齐、人名词典、ASS 排版）
  common.py    共用底座（工具路径解析、子进程、时间戳、SRT 读写、日志、doctor）

注：还没有把 pipeline 再细分（probe / mux / transcribe 各一个模块）——那是下一步的事，
现在这样已经能 pip 安装、能用 python -m anime_jp_sub 跑。
"""

__version__ = "0.2.0"

__all__ = ["__version__"]

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双击 run_jp_sub.bat 时走的入口：代码在 src/anime_jp_sub/（见那里的模块说明）。

为什么单独有这个文件：仓库用的是 src 布局，没 pip 安装的话包不在 import 路径里，
这里把 src/ 加进去，做到"克隆下来双击就能跑、不用先装"。

注意：不要把它命名成 anime_jp_sub.py —— 那样会和 src/anime_jp_sub/ 这个包重名，
`python -m anime_jp_sub` 会找到这个文件而不是包（踩过）。

装了包的话，也可以直接：  python -m anime_jp_sub process "D:/Anime/2026.7"
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from anime_jp_sub.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

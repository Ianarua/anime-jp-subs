#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容壳：注音模块现在在 src/anime_jp_sub/furigana.py。

单独用它生成 ASS 的用法不变：
    python furigana.py in.srt -o out.ass --video in.mkv
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from anime_jp_sub import furigana as _furigana  # noqa: E402

if __name__ == "__main__":
    sys.exit(_furigana.main())

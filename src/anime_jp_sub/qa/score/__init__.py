# -*- coding: utf-8 -*-
"""打分（B）：拿人工标出来的评审结果，给切分算法打分。

这套东西是这个项目的"验收标准"：先人工标一集（`qa/review` 导出），再改算法，
然后用这里的 `score()` 看通过率有没有涨。断句算法就是靠一集 80 条标记
从"一处都不对"改到"对 44 处"的。

用法（命令行）：
    anime-jp-sub score 标记.txt --baseline E11.jp.ass --dump E11.jp.dump.json
"""

from .marks import (map_lines_to_words, mark_seams, parse_marks, seam_time)
from .run import load_dump, report, score

__all__ = ["map_lines_to_words", "mark_seams", "parse_marks", "seam_time",
           "load_dump", "report", "score"]

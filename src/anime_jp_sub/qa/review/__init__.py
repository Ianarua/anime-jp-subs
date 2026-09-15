# -*- coding: utf-8 -*-
"""评审页（A）：把一集的日语字幕逐条列成网页 / 清单，边看边标"哪句断错了 / 哪句漏了"。

入口是 `generate(target, overwrite=False)`（`anime-jp-sub review <路径>` 走它）。
"""

from .build import (build_rows, flags_for, generate, generate_one, read_ass_entries,
                    render_html, render_txt, template_path)

__all__ = ["build_rows", "flags_for", "generate", "generate_one", "read_ass_entries",
           "render_html", "render_txt", "template_path"]

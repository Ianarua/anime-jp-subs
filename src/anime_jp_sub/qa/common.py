# -*- coding: utf-8 -*-
"""QA 工具共用的一小块：读字幕正文、时间解析。

评审（`qa/review`）和打分（`qa/score`）都会用到它——两边的输入输出完全不同，
只有"把一集字幕读成 [(起, 止, 文本)]"这件事是共用的，所以单独放这儿。
"""

import re
from pathlib import Path

from .. import common as app_common


def sec(text):
    """'0:00:07.85' → 7.85（ASS 的时间格式）。"""
    h, m, s = text.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def read_ass_entries(ass_path):
    """从 ASS 里抽出**正文**行。

    ⚠ 注音是**单独的 Dialogue 行**（样式 RB），必须排除——不排的话一条字幕会变成
    三条（正文 + 两段注音），评审页的行数和打分都会错。
    """
    rows = []
    for line in Path(ass_path).read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line[len("Dialogue:"):].split(",", 9)
        if parts[3].strip() not in ("JP", "Default"):
            continue
        text = re.sub(r"\{[^}]*\}", "", parts[9]).strip()
        if text:
            rows.append((sec(parts[1].strip()), sec(parts[2].strip()), text))
    return rows


def read_srt_entries(srt_path):
    """从 SRT 里抽正文行（`--no-furigana` 那条件就是 SRT）。"""
    text = Path(srt_path).read_text(encoding="utf-8-sig", errors="replace")
    rows = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        a, b = lines[1].split("-->")

        def to_sec(t):
            t = t.strip().replace(",", ".")
            h, m, s = t.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)

        body = "".join(lines[2:]).strip()
        if body:
            rows.append((to_sec(a), to_sec(b), body))
    return rows


def read_entries(path):
    """按扩展名选解析器（.ass / .srt）。"""
    p = Path(path)
    if p.suffix.lower() == ".srt":
        return read_srt_entries(p)
    return read_ass_entries(p)


def extract_subtitle(mkv, out_path, jpn_only=True):
    """把第一条日语字幕轨抽成文件（找不到日语轨就报错）。返回输出路径。"""
    from .. import pipeline

    _audios, subs = pipeline.probe_streams(str(mkv))
    idx = None
    for i, st in enumerate(subs):
        lang = (st.get("tags") or {}).get("language", "").lower()
        if lang in ("jpn", "ja", "japanese"):
            idx = i
            break
    if idx is None:
        raise RuntimeError("这集没有日语字幕轨（先跑 process，或换一集）")
    code, out = app_common.run([app_common.FFMPEG, "-v", "error", "-y", "-i", str(mkv),
                               "-map", "0:s:%d" % idx, "-c", "copy", str(out_path)])
    if code != 0:
        raise RuntimeError("抽出日语字幕失败：%s" % out)
    return out_path

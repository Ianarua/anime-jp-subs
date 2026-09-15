# -*- coding: utf-8 -*-
"""打分：一边是人工标记（验收标准），一边是当前算法的切分结果，算通过率。

口径（和当初调参时一致，别再改）：
  * **合并**标记：这个词缝上**不该**有切点；
  * **拆分**标记：这个词缝上**该**有切点；
  * 允许**差一个词缝**（±1）——人点的是"这两句该分开"，落在相邻词缝上阅读体验一样。

要用到的三样东西：
  1. `--baseline`：**人当时审的那份产物**（.ass 或 .srt）——标记的行号属于它；
  2. `--dump`：那一集的听写结果（`.jp.dump.json`，`process --keep-dump` 会写）；
  3. 标记文本（评审页导出）。
"""

import json
from pathlib import Path

from ... import pipeline
from ..common import read_entries
from .marks import map_lines_to_words, mark_seams, parse_marks, seam_time


def load_dump(path):
    """读听写 dump：{"segs": [...], "spans": [...], "pauses": [...]}。

    dump 是 `process --keep-dump` 写出来的，**存了听写结果和 VAD 结果**，
    所以打分不用再跑一遍 whisper（一集 1 分钟）。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    segs = [(float(s), float(e), t, [tuple(w) for w in ws]) for s, e, t, ws in data["segs"]]
    spans = [tuple(x) for x in data.get("spans", [])]
    pauses = [tuple(x) for x in data.get("pauses", [])]
    return segs, spans, pauses


def flatten_words(segs):
    return [w for _s, _e, _t, ws in segs for w in ws]


def cut_seams(lines, words):
    """当前产物在哪些词缝上有切点（按行文本贪心对齐）。"""
    cuts, cur = set(), 0
    for _s, _e, t in lines:
        acc = ""
        while cur < len(words) and len(acc) < len(t):
            acc += words[cur][2]
            cur += 1
        cuts.add(cur)
    cuts.discard(0)
    cuts.discard(len(words))
    return cuts


def _hit(seam, cuts, tol):
    return any((seam + d) in cuts for d in range(-tol, tol + 1))


def score(baseline_lines, words, marks, candidate_lines, tolerance=1):
    """算通过率。返回 dict（含逐条明细，方便 -v 打印）。"""
    rng = map_lines_to_words(baseline_lines, words)
    want_merge, want_split = mark_seams(marks, rng, words)
    cuts = cut_seams(candidate_lines, words)

    detail = []
    ok_merge = 0
    for seam in want_merge:
        ok = not _hit(seam, cuts, tolerance)
        ok_merge += ok
        detail.append({"kind": "merge", "seam": seam, "ok": ok,
                       "time": seam_time(words, seam)})
    ok_split = 0
    for seam in want_split:
        ok = _hit(seam, cuts, tolerance)
        ok_split += ok
        detail.append({"kind": "split", "seam": seam, "ok": ok,
                       "time": seam_time(words, seam)})
    total = len(want_merge) + len(want_split)
    passed = ok_merge + ok_split
    return {"lines": len(candidate_lines), "total": total, "passed": passed,
            "merge": {"ok": ok_merge, "n": len(want_merge)},
            "split": {"ok": ok_split, "n": len(want_split)},
            "cuts": len(cuts), "detail": detail}


def report(result, verbose=False):
    """人类可读的报告。`verbose` 会逐条列出没通过的那些（带时间码，方便回听）。"""
    pct = (result["passed"] / result["total"] * 100) if result["total"] else 0.0
    lines = ["打分（口径：允许差一个词缝）",
             "  行数 %d  切点 %d" % (result["lines"], result["cuts"]),
             "  不该断（合并）：%d/%d" % (result["merge"]["ok"], result["merge"]["n"]),
             "  该断（拆分）：  %d/%d" % (result["split"]["ok"], result["split"]["n"]),
             "  合计：%d/%d = %.1f%%" % (result["passed"], result["total"], pct)]
    if verbose:
        bad = [d for d in result["detail"] if not d["ok"]]
        if bad:
            lines.append("  没通过的 %d 处：" % len(bad))
            for d in sorted(bad, key=lambda x: x["time"]):
                m, s = divmod(d["time"], 60)
                what = "不该断却断了" if d["kind"] == "merge" else "该断却没断"
                lines.append("    %02d:%05.2f  %s（缝 #%d）" % (int(m), s, what, d["seam"]))
    return "\n".join(lines)


def run(marks_path, baseline_path, dump_path, tolerance=1, verbose=False):
    """命令行入口的实现：读三份输入 → 打分 → 打印。返回 0/2。"""
    baseline_lines = read_entries(baseline_path)
    segs, spans, pauses = load_dump(dump_path)
    words = flatten_words(segs)
    from ... import pipeline as _p
    candidate = _p.segment_lines(segs, spans, pauses, width=1920, height=1080, main_px=60)
    marks = parse_marks(Path(marks_path).read_text(encoding="utf-8"))
    result = score(baseline_lines, words, marks, candidate, tolerance=tolerance)
    print(report(result, verbose=verbose))
    return 0

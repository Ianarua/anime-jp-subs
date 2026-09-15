# -*- coding: utf-8 -*-
"""人工标记的解析，以及"标记 → 词缝"的映射。

评审页导出的文本长这样（一行一条标记）：

    合并 054-055   [05:23.21 ほうけてないで…] + [05:26.13 ハルキチー…]  → 应该是一条
    拆分 012 @ 第3字后   [02:29.90 近日に頼まれて]  → 前「近日に」/ 后「頼まれて」
    漏句 第009条之后 时间「00:38」
    备注 019  已经不是同一个人说的了

⚠ **标记的行号属于"人当时审的那份产物"**，所以映射必须拿那份产物（baseline）来算。
代码一改行号就漂，拿新产物去对旧行号会全错（踩过一次，整轮评分作废）。
"""

import re

MERGE_RE = re.compile(r"^合并\s+(\d+)-(\d+)", re.M)
SPLIT_RE = re.compile(r"^拆分\s+(\d+)\s*@\s*第(\d+)字后", re.M)
MISS_RE = re.compile(r"^漏句\s+", re.M)
NOTE_RE = re.compile(r"^备注\s+(\d+)", re.M)


def parse_marks(text):
    """解析标记文本 → {"merges": [(n, n+1)], "splits": [(n, 第几个字后)], "misses": n, "notes": n}。"""
    return {
        "merges": [(int(m.group(1)), int(m.group(2))) for m in MERGE_RE.finditer(text)],
        "splits": [(int(m.group(1)), int(m.group(2))) for m in SPLIT_RE.finditer(text)],
        "misses": len(MISS_RE.findall(text)),
        "notes": len(NOTE_RE.findall(text)),
    }


def map_lines_to_words(lines, words):
    """行 → 词下标区间：行文本 = 连续若干个词的拼接，贪心吃掉即可。

    返回 {行号: (起, 止)}（1-based 行号，止是开区间）。
    对不上就 assert —— 说明 baseline 和 words 不是同一份产物，早点炸比默默算错好。
    """
    rng, cur = {}, 0
    for i, (_s, _e, t) in enumerate(lines, 1):
        acc, start = "", cur
        while cur < len(words) and len(acc) < len(t):
            acc += words[cur][2]
            cur += 1
        assert acc.strip() == t.strip(), "第 %d 行对不上：「%s」 vs 「%s」" % (i, t, acc)
        rng[i] = (start, cur)
    assert cur == len(words), "只吃掉 %d/%d 个词（baseline 和 words 不是同一份产物？）" % (
        cur, len(words))
    return rng


def seam_time(words, idx):
    """第 idx 个词缝的时间（前一个词的结束、后一个词的开始，取中点）。"""
    if idx <= 0:
        return words[0][0]
    if idx >= len(words):
        return words[-1][1]
    return (words[idx - 1][1] + words[idx][0]) / 2


def _closest_word_index(words, i0, i1, k):
    """第 k 个字之后 → 离它最近的**词缝**下标（词的边界不一定正好落在那个字上）。"""
    acc, best_q, best_d = 0, None, None
    for q in range(i0, i1):
        acc += len(words[q][2])
        d = abs(acc - k)
        if best_d is None or d < best_d:
            best_q, best_d = q + 1, d
    return best_q


def mark_seams(marks, rng, words):
    """把标记翻成词缝下标：合并 → "这里不该有切点"，拆分 → "这里该有切点"。"""
    want_merge = [rng[n][1] for n, _n1 in marks["merges"]]
    want_split = []
    for n, k in marks["splits"]:
        i0, i1 = rng[n]
        seam = _closest_word_index(words, i0, i1, k)
        if seam is not None:
            want_split.append(seam)
    return want_merge, want_split

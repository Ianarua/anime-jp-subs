#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
furigana.py -- 给日语字幕自动加平假名注音，生成 ASS（给 PotPlayer 看）

为什么单独一个文件：注音是"显示层"的事，跟听写/对齐/内封无关，解耦了好单独调。

规则（与用户确认过）：
  * 汉字 -> 平假名（振假名），片假名 -> 平假名（查表转写），平假名本身不注
  * 一条字幕永远只有一行：文本全部用 \\pos 绝对定位，WrapStyle=2（只认显式换行），
    再按"本集最宽的条目"算一个全集统一字号，保证谁都放得下
  * 超过 MAX_CHARS 字的条目拆成前后相继的两条（时间按字数比例切）
  * 颜色沿用原片中文字幕：白字 + 黑边，不内封字体（用系统的 MS Gothic）
  * 人名词典：`表记<Tab>平假名`，最长优先匹配，放在番剧文件夹下，用来纠正词典查错的读音

用法:
    python furigana.py in.srt -o out.ass --size 1920x1080
    python furigana.py in.srt -o out.ass --dict "D:/Anime/2026.7/断后十年/furigana_dict.txt"
"""

import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

from . import common   # 工具路径 / 子进程 / 时间戳 / SRT 读写（见 common.py）

# ===== 可调参数 =====
# 字体：必须选"汉字/假名恒 1.00em、ASCII 恒 0.50em"的字体，坐标才能精确算出来。
# ⚠ 光看字体文件不够 —— 实测 Meiryo 在 libass/PotPlayer 下只有 0.665em、Yu Gothic 0.775em，
#   拿 1em 算坐标会整体错位。下面两个是实测渲染出来验证过的（差 <3px）。
# 字体随仓库分发（OFL 许可，可再分发）。换字体的前提是用 FontMetrics 量准字宽和 libass 的
# 缩放比——不再有"必须是 1em/0.5em 字体"的限制。
FONT = "M PLUS 1 Code"                       # ASS 里写的字体名（= 字体文件里的族名）
FONT_FILE_REL = Path("fonts") / "MPLUS1Code-VF.ttf"


def font_file():
    """随项目分发的字体文件路径；可用环境变量 ANIME_JP_SUB_FONT 换成别的字体。"""
    env = os.environ.get("ANIME_JP_SUB_FONT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent / FONT_FILE_REL

DICT_NAME = "furigana_dict.txt"      # 人名词典文件名（放番剧文件夹下，一个番剧一份）
DICT_MARK = "# ==== 候选（脚本自动生成，每次运行重写这一段）===="
DICT_USER_MARK = "# ==== 我的词典（你自己写，脚本不会动这一段）===="
DICT_HEADER = [
    "# furigana_dict.txt —— 日语注音用的人名/专有名词读音表",
    "# 用法：一行一条，左边写字幕里原样的字，右边写平假名读音，中间用 Tab 隔开。",
    "#   例：小玉<Tab>たま",
    "# 片假名不用写（片假名会自动转写成平假名，100% 准）；# 开头的行都是注释，不生效。",
    "# 改完直接跑流水线就行，词典每次运行都会重新读。",
]

# 字号一律按"占画面高度的比例"定义，这样 720p / 1080p / 4K 都保持同样的视觉大小
MAIN_HEIGHT_RATIO = 60 / 1080    # 主字幕（1080p 下 60px，用户实测选定）
RUBY_RATIO = 27 / 60             # 注音 = 主字幕的 45%
RUBY_MIN_RATIO = 0.30            # 注音最小比例（读音特别长的词不能再缩）
BOTTOM_RATIO = 50 / 1080         # 主字幕底边距（用户实测选定；按画面高度比例，换分辨率一致）
MARGIN_X_RATIO = 0.0208          # 左右安全边距（1920 下约 40px）
FALLBACK_MAX_CHARS = 40          # 算不出上限时的兜底（正常情况下按字号自动算）

MAIN_OUTLINE = 3            # 主字幕描边
RUBY_OUTLINE = 2            # 注音描边（要比主字幕细，否则糊成一团）
MAIN_BOLD = True            # 主字幕加粗（BIZ UDGothic 不加粗偏细）
RUBY_BOLD = True            # 注音加粗（不加粗在小字号下会糊）

# ===== 假名 =====
# 注意：str.translate 的表必须用"字符码(int)"做键，用 chr() 生成的字符串键会被静默忽略
KATA2HIRA = {c: c - 0x60 for c in range(0x30A1, 0x30F7)}              # ァ..ヴ -> ぁ..ゔ
KATA2HIRA.update({0x30F4: 0x3094, 0x30F5: 0x3095, 0x30F6: 0x3096})    # ヴ ヵ ヶ
KATA2HIRA[0x30FC] = 0x30FC                                            # 长音符「ー」保留

ITER_MARKS = "々ゝゞヽヾ〻"
RENDAKU = str.maketrans("かきくけこさしすせそたちつてとはひふへほ",
                        "がぎぐげござじずぜぞだぢづでどばびぶべぼ")

_KANJI = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF\u3005\u3006]")
_KATA = re.compile(r"[\u30A1-\u30FA\u30FC\uFF66-\uFF9D]")
_HIRA = re.compile(r"[\u3041-\u3096]")


def is_kanji(ch):
    return bool(_KANJI.match(ch))


def is_kata(ch):
    return bool(_KATA.match(ch))


def is_hira(ch):
    return bool(_HIRA.match(ch))


def to_hira(s):
    """片假名(含半角) -> 平假名。平假名/汉字原样返回。
    半角先走 NFKC 归一化（半角片假名到全角的映射不连续，不能用固定偏移）。"""
    return unicodedata.normalize("NFKC", s).translate(KATA2HIRA)


def norm_kana(ch):
    """把片假名统一成平假名再比较，方便对齐送假名。"""
    return to_hira(ch) if is_kata(ch) else ch


def char_em(ch, ratio=1.0):
    """字符宽度（em）——直接问字体文件要，不再假设 1em/0.5em。"""
    return METRICS.em(ch)


def text_em(s):
    return sum(char_em(c) for c in s)


def is_kana_like(ch):
    return is_hira(ch) or is_kata(ch)


# ===== 字体度量 =====
def read_font_metrics(path, index=0):
    """从字体文件读 (unitsPerEm, winAscent, winDescent)。TTC 也能读（按 index 取第几个字体）。"""
    import struct
    data = Path(path).read_bytes()
    if data[:4] == b"ttcf":
        off = struct.unpack(">I", data[12 + 4 * index:16 + 4 * index])[0]
    else:
        off = 0
    num_tables = struct.unpack(">H", data[off + 4:off + 6])[0]
    tables = {}
    for i in range(num_tables):
        p = off + 12 + 16 * i
        tag, _cs, o, _l = struct.unpack(">4sIII", data[p:p + 16])
        tables[tag] = o
    upem = struct.unpack(">H", data[tables[b"head"] + 18:tables[b"head"] + 20])[0]
    oo = tables[b"OS/2"]
    win_asc, win_desc = struct.unpack(">HH", data[oo + 74:oo + 78])
    return upem, win_asc, win_desc


class FontMetrics:
    """字体的"真实字宽"和 libass 的"渲染缩放"。

    为什么需要这个类（踩过两次的坑）：
      * 字宽不能假设"汉字 1em、ASCII 0.5em"——M PLUS 1 Code 的 `，．！？：；【】…―～`
        就是 0.5em，而 MS Gothic 里都是 1em；只能问字体文件。
      * **libass 会把字号按字体行高缩放**：缩放比 = unitsPerEm / (winAscent+winDescent)。
        实测 100px 字号下的实际字距：MS Gothic 100.0、BIZ UDGothic 100.0（行高比 1.000，
        所以以前没暴露）、Meiryo 66.5（1.500）、Yu Gothic 77.5（1.287）、M PLUS 1 Code 67.0（1.505）。
        所以 ASS 里要写 `目标像素 / scale`，排版的坐标才和实际渲染对得上。
    """

    def __init__(self, path):
        from PIL import ImageFont
        self.path = Path(path)
        self.upem, self.win_asc, self.win_desc = read_font_metrics(self.path)
        self.scale = self.upem / float(self.win_asc + self.win_desc)
        self._pil = ImageFont.truetype(str(self.path), 1000)   # 大字号量，减少取整误差
        self._cache = {}

    def em(self, ch):
        """单个字符的宽度（em，按字体真实度量）。"""
        w = self._cache.get(ch)
        if w is None:
            w = self._pil.getlength(ch) / 1000.0
            self._cache[ch] = w
        return w

    def ass_size(self, target_px):
        """想让字在画面上有 target_px 大时，ASS 的 Fontsize 该写多少。"""
        return target_px / self.scale


_METRICS = None


def metrics():
    """全局字体度量（用随项目分发的字体）。拿不到字体文件时退回旧的 1em/0.5em 假设。"""
    global _METRICS
    if _METRICS is None:
        try:
            _METRICS = FontMetrics(font_file())
        except Exception as e:                 # noqa: BLE001
            print(f"  [warn] 读不到字体 {font_file()}（{e}），"
                  f"退回 1em/0.5em 估算，注音位置可能偏")
            _METRICS = _FallbackMetrics()
    return _METRICS


class _FallbackMetrics:
    """字体文件缺失时的兜底：按老规矩算（汉字/假名 1em、ASCII 0.5em），并告警一次。"""

    scale = 1.0

    def em(self, ch):
        o = ord(ch)
        return 0.5 if (o < 0x0100 and 0x20 <= o < 0x7F) else 1.0

    def ass_size(self, target_px):
        return target_px


class _LazyMetrics:
    """模块级的 METRICS：第一次用到时才去读字体文件。"""

    def __getattr__(self, name):
        return getattr(metrics(), name)


METRICS = _LazyMetrics()


def rendaku(reading):
    """连浊：かみ -> がみ、ひ -> び。只用在叠字记号上（神々=かみがみ）。"""
    if not reading:
        return reading
    # Janome 给的是片假名读音，先归一成平假名再查连浊表
    return to_hira(reading[:1]).translate(RENDAKU) + reading[1:]


# ===== 读音 =====
def ruby_parts(surface, reading):
    """把读音按"送假名锚点"分配到汉字块，返回 [(起始, 长度, 读音), ...]。

    以表层里的假名为锚点，到读音里找同一个假名：两个锚点之间那段读音就归它前面
    的汉字块。这样前缀、后缀、词中夹假名三种情况用同一套逻辑就都对了：
      食べる / たべる   -> 食(た)
      連れ去り / つれさり -> 連(つ) れ 去(さ) り
      今日 / きょう     -> 今日(きょう)   （没有假名锚点，整词注一条）
    """
    parts = []
    j = 0
    block = None
    for i, ch in enumerate(surface):
        if is_kana_like(ch):
            k = reading.find(norm_kana(ch), j)
            if k >= 0:
                if block is not None and k > j:
                    parts.append((block, i - block, reading[j:k]))
                j = k + 1
                block = None
                continue
        if block is None:
            block = i
    if block is not None and j < len(reading):
        parts.append((block, len(surface) - block, reading[j:]))
    return [(a, n, r) for a, n, r in parts if r and n > 0]


def merge_iteration_marks(tokens):
    """Janome 偶尔把「神々」拆成 神 + 々（々 的读音不是假名）。合并后把读音连浊。"""
    merged = []
    for surface, reading in tokens:
        if merged and surface and all(c in ITER_MARKS for c in surface):
            prev_s, prev_r = merged[-1]
            if prev_r and all(is_kana_like(c) for c in prev_r):
                merged[-1] = (prev_s + surface, prev_r + rendaku(prev_r) * len(surface))
                continue
        merged.append((surface, reading))
    return merged


def spans_for_text(text, analyzer, dictionary):
    """把一行文本切成注音片段，返回 [(起始下标, 长度, 注音平假名), ...]。
    顺序扫描：先试词典最长匹配，没命中就交给 Janome 分词逐词处理。"""
    spans = []
    i = 0
    while i < len(text):
        hit = dictionary.match(text, i)          # 人名词典优先
        if hit:
            key, ruby = hit
            spans.append((i, len(key), ruby))
            i += len(key)
            continue
        # 找下一个词典命中点，中间的交给 Janome
        nxt = len(text)
        for k in range(i + 1, len(text)):
            if dictionary.match(text, k):
                nxt = k
                break
        seg = text[i:nxt]
        for off, ln, ruby in spans_for_segment(seg, analyzer):
            spans.append((i + off, ln, ruby))
        i = nxt
    return spans


def spans_for_segment(seg, analyzer):
    """单个片段（无词典命中）交给 Janome。"""
    out = []
    pos = 0
    toks = merge_iteration_marks([(t.surface, t.reading or "*") for t in analyzer.tokenize(seg)])
    for surface, reading in toks:
        start = seg.find(surface, pos)           # 用 find 兜底，防止 Janome 改写字面
        if start < 0:
            start = pos
        pos = start + len(surface)

        kana_reading = reading != "*" and all(is_kana_like(c) or c in "ー" for c in reading)
        if any(is_kanji(c) for c in surface) and kana_reading:
            for off, ln, ruby in ruby_parts(surface, to_hira(reading)):
                out.append((start + off, ln, ruby))
        elif surface and all(is_kata(c) for c in surface):
            # 片假名：读音就是它自己，转平假名即可（Janome 这类词素的 reading 是 *）
            out.append((start, len(surface), to_hira(surface)))
    return out


class Dictionary:
    """人名词典，纯文本，一行一条：`表记<Tab>平假名`，`#` 开头是注释。

    放在番剧文件夹下（`furigana_dict.txt`），用来纠正字典查错的读音——尤其是人名。
    匹配是**最长优先**、且在分词之前于原文上匹配，所以「小玉」这种被 Janome 拆成
    「小」+「玉」的词也能整串命中。
    """

    def __init__(self, entries=None):
        self.entries = entries or {}
        self.keys = sorted(self.entries, key=len, reverse=True)

    @classmethod
    def load(cls, path):
        """path 可以是词典文件，也可以只给番剧文件夹（自动找 furigana_dict.txt）。"""
        p = Path(path)
        if p.is_dir():
            p = p / DICT_NAME
        return cls(cls._read(p))

    @classmethod
    def _read(cls, p):
        if not p.is_file():
            return {}
        return parse_dict_text(p.read_text(encoding="utf-8-sig"))

    def match(self, text, i):
        """在原文第 i 个字符处做最长优先匹配，命中返回 (表记, 读音)，否则 None。"""
        for k in self.keys:
            if text.startswith(k, i):
                return k, self.entries[k]
        return None


def parse_dict_text(text):
    """解析词典文本里的生效条目（# 开头的行是注释）。也用来判断候选里哪些已经写过了。"""
    entries = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 只取前两段：后面再有内容（比如候选行带的"出现 N 次"）一律忽略，
        # 这样候选行去掉行首 # 就能直接当生效条目用。
        parts = re.split(r"[\t,，\s]+", line, maxsplit=2)
        if len(parts) >= 2 and parts[0] and parts[1]:
            entries[parts[0]] = parts[1].strip()
    return entries


class Analyzer:
    def __init__(self):
        from janome.tokenizer import Tokenizer
        self.tk = Tokenizer()

    def tokenize(self, text):
        return list(self.tk.tokenize(text))


def collect_candidates(entries, limit=40, min_len=2):
    """扫一遍字幕，挑出**含汉字的固有名詞**（人名 / 地名 / 組織名 / 一般），
    返回 [(表记, 字典读音或 "?", 出现次数)]，按出现次数从多到少。

    为什么要挑这些：片假名会自动转写成平假名不需要人管，需要人核对读音的正是
    汉字写的专有名词——字典（IPADIC）对人名地名的读音经常猜错，而且一错就是一整季。
    """
    from collections import Counter
    counts, readings = Counter(), {}
    an = Analyzer()
    for seg in entries:
        text = seg[2] if len(seg) > 2 else ""
        if not text:
            continue
        for tok in an.tokenize(text):
            if not tok.part_of_speech.startswith("名詞,固有名詞"):
                continue
            surface = tok.surface
            if len(surface) < min_len or not any(is_kanji(c) for c in surface):
                continue                       # 片假名/短词不用管
            counts[surface] += 1
            if surface not in readings:
                readings[surface] = to_hira(tok.reading) if tok.reading not in ("", "*") else "?"
    ranked = counts.most_common(limit)
    return [(s, readings.get(s, "?"), n) for s, n in ranked]


def update_dict(folder, candidates):
    """没有词典就按模板新建；已有的话只重写文件末尾的"候选"段，
    用户自己写的条目一律不动。返回 (词典路径, 是否改动, 候选条数)。"""
    p = Path(folder) / DICT_NAME
    if p.is_file():
        old = p.read_text(encoding="utf-8-sig")
        keep = old.split(DICT_MARK)[0].rstrip() + "\n"
    else:
        keep = "\n".join(DICT_HEADER) + "\n\n" + DICT_USER_MARK + "\n"

    # 已经写进「我的词典」的词就别再当候选提醒了
    done = set(parse_dict_text(keep))
    candidates = [c for c in candidates if c[0] not in done]

    body = [DICT_MARK,
            "# 下面是从本片字幕里挑出的含汉字固有名詞（括号里是字典给的读音，可能是错的）。",
            "# 核对后把行首的 # 去掉、把读音改对，它就成了生效条目；排在前面的是出现次数多的。"]
    if candidates:
        for surface, reading, n in candidates:
            body.append(f"#{surface}\t{reading}\t出现 {n} 次")
    else:
        body.append("# （本次没挑到含汉字的固有名詞）")
    new = keep + "\n" + "\n".join(body) + "\n"

    if p.is_file() and old == new:
        return p, False, len(candidates)
    p.write_text(new, encoding="utf-8")
    return p, True, len(candidates)


# ===== 长句拆分 =====
SPLIT_PUNCT = "、。！？!?…「」"
SPLIT_PARTICLES = ("から", "まで", "けど", "ので", "のに", "って", "という",
                   "は", "が", "を", "に", "で", "と", "も", "て", "し", "ば")


def split_long(text, limit=FALLBACK_MAX_CHARS):
    """超过 limit 字就拆成两条。切点尽量落在标点/助词后、且靠近中间。"""
    if len(text) <= limit:
        return [text]
    mid = len(text) / 2
    best, best_score = None, None
    for i in range(8, len(text) - 8):
        score = abs(i - mid)
        if text[i - 1] in SPLIT_PUNCT:
            score -= 12                      # 标点后断句最好
        else:
            for p in SPLIT_PARTICLES:
                if text[max(0, i - len(p)):i] == p:
                    score -= 6
                    break
        if best_score is None or score < best_score:
            best, best_score = i, score
    if best is None:
        best = int(mid)
    head, tail = text[:best], text[best:]
    # 两边都可能还超长，递归
    return split_long(head, limit) + split_long(tail, limit)


def expand_long(entries, limit=FALLBACK_MAX_CHARS):
    """把超长条目拆成前后相继的多条；时间按字数比例切。"""
    out = []
    for s, e, text in entries:
        parts = split_long(text, limit)
        if len(parts) == 1:
            out.append((s, e, text))
            continue
        total = sum(len(p) for p in parts)
        t = s
        for p in parts:
            d = (e - s) * len(p) / total
            out.append((t, t + d, p))
            t += d
    return out


# ===== 排版 =====
def margin_x(width):
    return int(round(width * MARGIN_X_RATIO))


def font_sizes(width, height, main_px=None):
    """字号按"占画面高度的比例"来定：720p / 1080p / 4K 下视觉大小一致。
    1080p 时主字幕 = 76px、注音 = 34px；main_px 可强制指定主字号（试字号用）。"""
    if main_px:
        main = max(20, int(main_px))
    else:
        main = max(20, int(round(height * MAIN_HEIGHT_RATIO)))
    ruby = max(12, int(round(main * RUBY_RATIO)))
    return main, ruby


def limits(width, height, main_px=None):
    """返回 (主字号, 注音字号, 每行最多几个字)。"""
    main, ruby = font_sizes(width, height, main_px)
    avail = width - 2 * margin_x(width)
    return main, ruby, max(8, int(avail // main))


def plan_layout(entries, width, height, main_px=None):
    """全集统一字号。按画面高度定基准，再按"最宽的那条"兜底缩小，
    保证每条都能单行放下（正常情况不会触发缩小）。"""
    main, ruby = font_sizes(width, height, main_px)
    avail = width - 2 * margin_x(width)
    # 只用"主文本"的宽度判断，不含注音溢出——否则一个读音特别长的词会把整集字号拖小
    widest = max((text_em(t) for _, _, t, _ in entries), default=0.0)
    if widest and widest * main > avail:
        main = max(24, int(avail / widest))
        ruby = max(12, int(main * RUBY_RATIO))
    return main, ruby


def fit_ruby_size(ruby, span_px, main_size, ruby_size):
    """单个注音的可用字号：默认 45%，比它盖住的汉字还宽时按比例缩，
    但不低于主字的 30%（再小就看不清了，宁可让它稍微出格）。"""
    if span_px <= 0:
        return ruby_size
    w_em = text_em(ruby)
    if w_em * ruby_size <= span_px:
        return ruby_size
    return max(int(main_size * RUBY_MIN_RATIO), int(span_px / w_em))


def line_width_em(text, spans):
    """整行占宽（em）。含注音向两端溢出的部分。"""
    xs = []
    off = 0.0
    for ch in text:
        xs.append(off)
        off += char_em(ch)
    total = off
    if spans:
        for start, ln, ruby in spans:
            span_w = sum(char_em(c) for c in text[start:start + ln])
            ruby_w = text_em(ruby) * RUBY_RATIO      # 注音按比例缩过，宽度要一起算
            over = (ruby_w - span_w) / 2.0
            if over > 0:
                total = max(total, xs[start] + span_w + over)
    return total


def build_ass(entries, width, height, dict_path=None, main_px=None, max_chars=None,
              bottom_px=None):
    """由 (start, end, text) 列表生成 ASS 文本，返回 (ass, 主字号, 注音字号)。
    内部会先按字号算出的每行上限把超长句拆成前后相继的多条，再排版——
    保证不管从命令行还是从主脚本调用，字号都不会被长句拖小。"""
    _, _, cap = limits(width, height, main_px)
    entries = expand_long(entries, max_chars or cap)
    analyzer = Analyzer()
    dictionary = Dictionary.load(dict_path) if dict_path else Dictionary()

    prepared = []
    for s, e, text in entries:
        spans = spans_for_text(text, analyzer, dictionary)
        prepared.append((s, e, text, spans))

    main_size, ruby_size = plan_layout(prepared, width, height, main_px)
    bottom = bottom_px if bottom_px else int(round(height * BOTTOM_RATIO))
    main_top = height - bottom - main_size
    ruby_top = main_top - int(ruby_size * 1.25)
    # ASS 里的 Fontsize 要做"渲染缩放"补偿：libass 按行高缩放字号，写 60 不一定渲染出 60
    ass_main = METRICS.ass_size(main_size)
    ass_ruby = METRICS.ass_size(ruby_size)

    head = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",                    # 只认显式 \N：绝不让播放器自己折行
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.601",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        f"Style: JP,{FONT},{ass_main:.1f},&H00FFFFFF,&H000000FF,&H00000000,&H7F000000,"
        f"{'-1' if MAIN_BOLD else '0'},0,0,0,100,100,0,0,1,{MAIN_OUTLINE},0,7,0,0,0,1",
        f"Style: RB,{FONT},{ass_ruby:.1f},&H00FFFFFF,&H000000FF,&H00000000,&H7F000000,"
        f"{'-1' if RUBY_BOLD else '0'},0,0,0,100,100,0,0,1,{RUBY_OUTLINE},0,7,0,0,0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    lines = list(head)

    for s, e, text, spans in prepared:
        # 居中只按主文本宽度算（注音允许向两端轻微溢出，不参与居中，免得整行被推歪）
        w_em = text_em(text)
        x0 = (width - w_em * main_size) / 2.0
        lines.append(f"Dialogue: 0,{common.fmt_ass_ts(s)},{common.fmt_ass_ts(e)},JP,,0,0,0,,"
                     f"{{\\an7\\pos({x0:.1f},{main_top:.1f})}}{escape(text)}")
        # 逐字累计 x 坐标，注音取自己覆盖区间的中心
        xs, off = [], 0.0
        for ch in text:
            xs.append(off * main_size + x0)
            off += char_em(ch)
        for start, ln, ruby in spans:
            span_x = xs[start]
            span_w = sum(char_em(c) for c in text[start:start + ln]) * main_size
            rs = fit_ruby_size(ruby, span_w, main_size, ruby_size)
            ruby_w = text_em(ruby) * rs
            rx = span_x + (span_w - ruby_w) / 2.0
            # 兜底：别让注音跑出画面
            rx = min(max(rx, 6.0), width - ruby_w - 6.0)
            fs = "" if rs == ruby_size else f"\\fs{METRICS.ass_size(rs):.1f}"
            lines.append(f"Dialogue: 0,{common.fmt_ass_ts(s)},{common.fmt_ass_ts(e)},RB,,0,0,0,,"
                         f"{{\\an7\\pos({rx:.1f},{ruby_top:.1f}){fs}}}{escape(ruby)}")

    return "\n".join(lines) + "\n", main_size, ruby_size


def escape(s):
    return s.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def main():
    ap = argparse.ArgumentParser(description="给日语 srt 加平假名注音，输出 ASS")
    ap.add_argument("srt")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--video", default=None,
                    help="片源 mkv：按它的真实分辨率生成 PlayRes（推荐，换片源/换分辨率都不会错）")
    ap.add_argument("--size", default="1920x1080", help="不给 --video 时用的 PlayRes")
    ap.add_argument("--dict", dest="dict_path", default=None, help="人名词典路径")
    ap.add_argument("--max-chars", type=int, default=None,
                    help="每行最多几个字；不填则按字号自动算")
    ap.add_argument("--main-px", type=int, default=None,
                    help="强制指定主字幕像素字号（试字号用；不填则按画面高度比例）")
    ap.add_argument("--bottom-px", type=int, default=None,
                    help="主字幕底边距（试位置用；不填则按画面高度比例）")
    args = ap.parse_args()

    m = metrics()          # 读字体度量（字宽 + libass 缩放比），读不到会告警

    if args.video:
        w, h = video_size(args.video)
    else:
        w, h = (int(x) for x in args.size.lower().split("x"))

    _, _, cap = limits(w, h, args.main_px)
    if args.max_chars:
        cap = args.max_chars
    entries = expand_long(common.load_srt(args.srt), cap)      # 只为打印条数，build_ass 内部还会再来一次(幂等)
    ass, ms, rs = build_ass(entries, w, h, args.dict_path, args.main_px,
                            args.max_chars, args.bottom_px)
    Path(args.out).write_text(ass, encoding="utf-8")
    print(f"[furigana] {len(entries)} 条 -> {args.out}")
    print(f"[furigana] 字体 {FONT}（{'粗体' if MAIN_BOLD else '常规'}）/ "
          f"主字幕 {ms}px / 注音 {rs}px / 每行上限 {cap} 字 / PlayRes {w}x{h} / "
          f"行高比 {1 / m.scale:.3f}（ASS 字号已补偿）")
    return 0


def video_size(mkv):
    """ffprobe 读片源分辨率，用它做 PlayRes。"""
    code, out = common.run([common.FFPROBE, "-v", "quiet", "-print_format", "json",
                            "-select_streams", "v:0", "-show_streams", str(mkv)])
    if code != 0:
        _, err = common.run([common.FFPROBE, "-v", "error", "-show_streams", str(mkv)])
        raise RuntimeError(f"ffprobe 读分辨率失败: {' '.join(err.split())[:160]}")
    streams = json.loads(out).get("streams") or []
    if not streams:
        raise RuntimeError(f"读不到视频轨: {mkv}")
    return int(streams[0]["width"]), int(streams[0]["height"])


if __name__ == "__main__":
    sys.exit(main())

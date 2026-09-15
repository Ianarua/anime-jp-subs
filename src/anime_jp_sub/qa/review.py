# -*- coding: utf-8 -*-
"""评审页：把一集的日语字幕逐条列出来，边看动画边标"哪句断错了 / 哪句漏了"。

为什么要有这个模块：字幕**断句**的质量只能靠耳朵判，机器测不出来。把"人工逐条审核"
做成一条命令，标完导出一段纯文本，就能当下一次调参的验收标准——这个项目的断句算法
就是靠这份人工标记从 0/80 改到 44/80 的（过程见 AGENTS.md）。

产物写在每集 mkv **旁边**（不改动 mkv 本身，删掉即还原）：
  * `<集名>.review.html` —— 边看边标用的页面：单文件、零依赖、双击就开，
    标记存在浏览器 localStorage 里，点「导出结果」复制出来
  * `<集名>.review.txt` —— 同样的内容，纯文本（不想开浏览器时用）

用法：
    anime-jp-sub review "D:/Anime/2026.7"        # 整个季度文件夹
    anime-jp-sub review "D:/Anime/2026.7/xxx.mkv"
"""

import json
import re
import shutil
import tempfile
from pathlib import Path

from .. import common
from .. import pipeline

# 行尾是这些 → 像一句话说完（跟断句算法里用的一致）
END_PUNCT = "。！？…?!"
# 页面里给"可疑点"用的阈值：跨度大或字数多的条目值得多看一眼
LONG_SEC = 8.0
LONG_CHARS = 24


def _long_pauses(pauses, minimum=1.5):
    """停顿列表里"长静音"的部分（>= minimum 秒）。"""
    return [(a, b) for a, b, g in pauses if g >= minimum]


def flags_for(text, start, end, long_pauses):
    """这一条的可疑点（只针对**切分**，不管识别对错）。

    ⚠跨停顿 = 这一条的显示区间盖住了一整段长静音（很可能"两句被并成一句"）
    ⚠行首助詞 = 这一行从助詞/助動詞开头（断点很可能该往前挪）
    ⚠行尾起句 = 行尾是「いや/あの/え/はい」这种只会起句的词（该归下一行）
    ⚠偏长 = 显示超过 8 秒或 24 字
    """
    out = []
    covered = [(a, b) for a, b in long_pauses if start < a and end > b]
    if covered:
        out.append("⚠跨停顿 %.1fs" % max(b - a for a, b in covered))
    if pipeline._bad_line_start(text):
        out.append("⚠行首助詞")
    if pipeline._ends_with_sentence_starter(text) and not pipeline._all_starters(text):
        out.append("⚠行尾起句")
    if end - start > LONG_SEC or len(text) >= LONG_CHARS:
        out.append("⚠偏长 %.1fs" % (end - start))
    return out


def build_rows(entries, pauses):
    """entries = [(start, end, text)] → 页面用的行数据。"""
    longs = _long_pauses(pauses)
    rows = []
    for i, (s, e, t) in enumerate(entries, 1):
        rows.append({"n": i, "s": round(float(s), 2), "e": round(float(e), 2),
                     "t": t, "flag": " ".join(flags_for(t, float(s), float(e), longs))})
    return rows


def _sec(text):
    """'0:00:07.85' → 7.85"""
    h, m, s = text.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def read_ass_entries(ass_path):
    """从 ASS 里抽出**正文**行（注音是单独的行、样式是 RB，必须排除）。"""
    rows = []
    for line in Path(ass_path).read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line[len("Dialogue:"):].split(",", 9)
        if parts[3].strip() not in ("JP", "Default"):
            continue
        text = re.sub(r"\{[^}]*\}", "", parts[9]).strip()
        if text:
            rows.append((_sec(parts[1].strip()), _sec(parts[2].strip()), text))
    return rows


def render_txt(rows, title):
    """纯文本版清单（每行：编号 时间 时长 标记 文本）。"""
    lines = ["# %s —— 日语字幕逐条清单" % title,
             "# 在行尾【】里写一句话就行，例如 【该和上一条并成一句】/【开头 ス 应归上一条】",
             "# ⚠ 是自动标出来的可疑点：跨停顿=这一条盖住了一段长静音（最可能是两句并成一句）",
             ""]
    for r in rows:
        m, s = divmod(r["e"], 60)
        h, m = divmod(m, 60)
        lines.append("%03d  %02d:%02d:%05.2f  %4.1fs  %-12s %s   【】"
                     % (r["n"], h, m, s, r["e"] - r["s"], r["flag"], r["t"]))
    return "\n".join(lines) + "\n"


def template_path():
    """页面模板（随包分发）。"""
    return Path(__file__).with_name("review_template.html")


def render_html(rows, title, key=None):
    """把数据注进模板，返回**单个 HTML 文件**的文本。"""
    tpl = template_path().read_text(encoding="utf-8")
    meta = {"title": title, "key": key or ("anime-jp-sub/review/" + title)}
    return (tpl.replace("/*__META__*/", json.dumps(meta, ensure_ascii=False))
               .replace("/*__DATA__*/", json.dumps(rows, ensure_ascii=False)))


def jpn_subtitle_stream(mkv):
    """(ffmpeg 用的字幕序号, 语言标签)：挑第一条日语字幕轨；没有就返回 (None, None)。"""
    _audios, subs = pipeline.probe_streams(mkv)
    for idx, st in enumerate(subs):
        lang = (st.get("tags") or {}).get("language", "").lower()
        if lang in ("jpn", "ja", "japanese"):
            return idx, lang
    return None, None


def generate_one(mkv, overwrite=False, keep_wav=False):
    """给一集 mkv 生成评审页（HTML + TXT），返回 (html 路径, txt 路径, 条目数)。"""
    mkv = Path(mkv)
    html_path = mkv.with_suffix(".review.html")
    txt_path = mkv.with_suffix(".review.txt")
    if html_path.exists() and not overwrite:
        return html_path, txt_path, 0

    sub_idx, lang = jpn_subtitle_stream(str(mkv))
    if sub_idx is None:
        raise RuntimeError("这集没有日语字幕轨（先跑 process，或换一集）")
    tmp = Path(tempfile.mkdtemp(prefix="anime-jp-sub-review-"))
    try:
        sub_path = tmp / "sub.ass"
        code, out = common.run([common.FFMPEG, "-v", "error", "-y", "-i", str(mkv),
                                "-map", "0:s:%d" % sub_idx, "-c", "copy", str(sub_path)])
        if code != 0:
            raise RuntimeError("抽出日语字幕失败：%s" % out)
        entries = read_ass_entries(sub_path)
        # 停顿用音频现算（跟断句用的是同一套 silero VAD 参数）——页面上的 ⚠跨停顿 靠它
        audios, _subs = pipeline.probe_streams(str(mkv))
        audio = pipeline.pick_audio(str(mkv), audios)
        wav = tmp / "a.wav"
        if audio is None:
            pauses = []
        else:
            pipeline.extract_wav(str(mkv), audio, str(wav))
            _spans, pauses = pipeline.detect_pauses(str(wav))
    finally:
        if not keep_wav:
            shutil.rmtree(tmp, ignore_errors=True)

    title = mkv.stem
    rows = build_rows(entries, pauses)
    html_path.write_text(render_html(rows, title), encoding="utf-8")
    txt_path.write_text(render_txt(rows, title), encoding="utf-8")
    return html_path, txt_path, len(rows)


def collect_mkvs(target):
    """目标可以是单集 mkv，也可以是文件夹（递归找 mkv）。"""
    target = Path(target)
    if target.is_file():
        return [target] if target.suffix.lower() == ".mkv" else []
    return sorted(target.rglob("*.mkv"))


def generate(target, overwrite=False, keep_wav=False):
    """给目标（文件夹或单集）里每个 mkv 生成评审页。返回 0/1。"""
    mkvs = collect_mkvs(target)
    if not mkvs:
        print("没找到 mkv：%s" % target)
        return 2
    done = skipped = 0
    failed = 0
    for mkv in mkvs:
        try:
            html_path, _txt, n = generate_one(mkv, overwrite=overwrite, keep_wav=keep_wav)
        except Exception as e:                                  # noqa: BLE001
            print("  跳过 %s：%s" % (mkv.name, e))
            failed += 1
            continue
        if n:
            print("  %s：%d 条 → %s" % (mkv.stem, n, html_path.name))
            done += 1
        else:
            skipped += 1
    print("生成 %d 集评审页，跳过 %d 集（已有，想重做加 --force），失败 %d 集"
          % (done, skipped, failed))
    return 0

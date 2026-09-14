#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
anime-jp-sub  --  给日语 mkv 自动生成并内封日语字幕

为什么做这条路：RSS 下载的字幕组内封全是"从日语翻译出去"的多国语言
(Chinese/English/Indonesian...)，唯独没有日语——因为日语是原声源头。
本脚本用 faster-whisper large-v3 听写日语音频，生成 .srt，再 mkvmerge
内封进 mkv，让 PotPlayer 能日语/中文对照切换。

关键设计（与用户对齐）：
1. 判定依据 = 是否已有 jpn(日语) 字幕轨：有=已处理跳过，没有=处理。
   内容判定，天然幂等；不依赖 mtime 时间闸(旧闸会把"下载早于上次运行但还没
   处理"的文件误判为已处理而漏做)。
2. stream copy 内封：视频/音频零重编码，一帧不动，只新增一条字幕轨。
3. srt 用完即弃(默认)；--keep-srt 可保留(测试/调试用)。
4. 默认扫目标文件夹下所有 mkv；支持命令行传季度文件夹或单集。
5. 正确性 = 逐句时间轴校验(以中文轨断句为基准) + 上限保护，
   杜绝"时间轴放大到几十小时"的bug。
6. 断句对齐 = 用中文字幕的断句边界去切 Whisper 的"词级时间轴"(word_timestamps)：
   词中点落在哪条中文台词里就归哪条，落空的词保留 Whisper 原时间单独成条。
   不再是"整段吸附到一条中文"——那会把两句并成一条长句、让另一条彻底没字幕。

用法:
    python anime_jp_sub.py                     # 扫当前目录(季度文件夹双击)
    python anime_jp_sub.py "D:/Anime/2026.7"   # 指定季度文件夹
    python anime_jp_sub.py "D:/Anime/2026.7/xxx.mkv"  # 单集
    python anime_jp_sub.py --all               # 忽略时间闸，全量重扫
    python anime_jp_sub.py --keep-srt          # 网格测试：保留 srt 调试
"""

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path

from . import common   # 工具路径解析 + 子进程 + 时间戳（见 common.py）

# 注音模块(同目录)。它依赖 janome / pillow，缺了就退回纯 SRT，不影响主流程。
try:
    from . import furigana
    HAVE_FURIGANA = True
    FURIGANA_ERR = ""
except Exception as _e:            # noqa: BLE001
    HAVE_FURIGANA = False
    FURIGANA_ERR = str(_e)

def find_dict(folder):
    """在番剧文件夹里找人名词典 furigana_dict.txt，返回路径或 None。"""
    p = Path(folder) / "furigana_dict.txt"
    return p if p.is_file() else None


def cfg_int(section, key, folder):
    """从配置读一个整数（番剧文件夹里的 config.ini 会覆盖全局的）。没有/写错就返回 None。"""
    raw = common.setting(section, key, None, folder)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        print(f"  [warn] 配置 {section}.{key} = {raw!r} 不是整数，按默认值处理")
        return None

# 工具链路径由 common 统一解析（环境变量 → config.ini → PATH → 项目 tools\），见 common.py

# 上次运行时刻快照(唯一持久状态)
LAST_RUN_FILE = Path(__file__).parent / ".last_run.json"

# ===== 识别参数 =====
LANG = "ja"
MODEL_SIZE = "large-v3"
COMPUTE_TYPE = "int8"     # 6G 显存，int8 最省
BEAM_SIZE = 1             # greedy 最快
VAD_FILTER = True
# 听写时"按换气切块"的参数（不是 detect_pauses 那套，两处的用途不同）：
#   min_silence_duration_ms: faster-whisper 默认 2000ms —— 说话中间 0.3~0.7s 的换气
#     会被并进同一块，块内的 DTW 就把词级时间拉歪了（实测词缝和真停顿偏移中位 +0.28s）。
#     改成 250ms 后偏移降到 -0.07s，±0.15s 命中率 45/525 → 318/525（见 README/AGENTS）。
#   speech_pad_ms: 默认 400ms 会把前后静音也切进块里，缩到 50ms 让边界贴住真停顿。
#   max_speech_duration_s: 8s，防止一口气说很长时一整段不分块。
VAD_PARAMS = {"threshold": 0.5, "min_speech_duration_ms": 100,
              "max_speech_duration_s": 8.0, "min_silence_duration_ms": 250,
              "speech_pad_ms": 50}

# 判定为"中文"字幕轨的语言标签(含简繁体常见变体)
CHINESE_LANGS = ("chi", "zh", "chs", "cht", "zh-cn", "zh-tw", "zh-hans", "zh-hant")

# 断句修复用：Whisper 给的是 BPE 碎片，日语没有空格，一个词常被切成 2 段
# （スポーツ→ス+ポーツ、実は→実+は）。中文边界正好切在词内部时，前半截会挂到上一句末尾。
GAP_WORD = 0.15        # 两个碎片最多隔多久还算"挨着"（同一个词被切开的典型特征）
CUT_SLACK = 0.12       # 中文边界最多贴到碎片末尾多远，仍算"切在词内部"
_PUNCT = "、。！？…「」『』，．,.!?（()）"
# 这些助詞不可能出现在一行字幕的开头：格助詞（が・を・に・へ…）、係助詞（は・も）、
# 連体化の、準体助詞の、副助詞（だけ・しか・ほど…）。出现就说明中文句界切错地方了。
_NO_LINE_START_PARTICLES = ("格助詞", "係助詞", "連体化", "準体助詞", "副助詞")
_JANOME = None


def _is_particle_like(pos, index):
    """Janome 的助詞子类可能是「副助詞／並立助詞／終助詞」这种斜杠串，要按串里的每一项比。"""
    return pos[0] == "助詞" and index < len(pos) and \
        any(sub in pos[index].split("／") for sub in _NO_LINE_START_PARTICLES)


def _false_particle(toks, i):
    """这个"助詞"其实是**某个词被拆开的前半截**吗？实测两种形态：

    ① 「という」：Janome 拆成 `と`(格助詞,引用)+`いう`(動詞)，不拦的话「というか…」开头的
       行会被当成"助詞起句"白吃重罚（实测把「…ですか。というか、俺…」断成「…ですという」
       「か俺…」）。
    ② 「はいいつきさん」「でも」：**碎片上下文**里 Janome 会把 `はい`/`でも` 拆成
       `は`+`いい`、`で`+`も`，于是"下一行以助詞开头"的规则误报——用户实测的
       "开头词被并进上一句"里，31 处拦截有 20 处是这类误报。

    判法：把"助詞 + 后一个词素"拼起来重新分词；如果合成一个 token 而它不是助詞/助動詞/接尾，
    说明刚才那个"助詞"只是别的词的头一个字。
    """
    if i < 0 or i >= len(toks):
        return False
    t = toks[i]
    if t.part_of_speech.split(",")[0] != "助詞":
        return False
    if i + 1 >= len(toks):
        return False
    nxt = toks[i + 1]
    if t.surface == "と" and nxt.part_of_speech.split(",")[0] == "動詞" \
            and nxt.base_form in ("いう", "言う"):
        return True                                    # ① という
    pair = t.surface + nxt.surface
    try:
        ptoks = list(_janome().tokenize(pair))
    except Exception:                                  # noqa: BLE001
        return False
    if len(ptoks) != 1 or ptoks[0].surface != pair:
        return False
    pos = ptoks[0].part_of_speech.split(",")
    if pos[0] in ("助詞", "助動詞"):
        return False
    if pos[0] == "名詞" and len(pos) > 1 and "接尾" in pos[1]:
        return False
    return True                                        # ② はい・でも・ねえ…


def _janome():
    """懒加载 Janome（只用它判断"两个碎片拼起来是不是一个词"）。"""
    global _JANOME
    if _JANOME is None:
        from janome.tokenizer import Tokenizer
        _JANOME = Tokenizer()
    return _JANOME


@lru_cache(maxsize=8192)
def _joined_one_word(a, b):
    """a 末尾 + b 开头 拼起来在 Janome 里是"一个词"吗。"""
    if not a or not b or a[-1] in _PUNCT or b[0] in _PUNCT:
        return False
    joined = a + b
    try:
        toks = list(_janome().tokenize(joined))
    except Exception:                               # noqa: BLE001
        return False
    return len(toks) == 1 and toks[0].surface == joined


@lru_cache(maxsize=8192)
def _is_bound_tail(t):
    """t 自己是贴在前面的助詞/助動詞（だ・な・ね 之类）——它可能就是上一句的正常句尾，
    这种不动，免得把上一句的句尾搬到下一句去。"""
    try:
        toks = list(_janome().tokenize(t))
    except Exception:                               # noqa: BLE001
        return False
    if len(toks) != 1:
        return False
    return toks[0].part_of_speech.split(",")[0] in ("助詞", "助動詞")


@lru_cache(maxsize=8192)
def _next_starts_with_particle(a, right_head):
    """下一条字幕开头是不是**格助詞/係助詞**（が・を・に・へ・で・と・は・も…）。

    句子不可能从格助詞开头，所以出现这种情况说明中文句界切错地方了——前一个词
    应该跟过去（实测：「…ですか俺」+「が」= 应该是「俺が」）。
    只认格助詞/係助詞：なあ/ても/のに 这类开头的句子是真的存在，不能动。

    做法：把 a 拼到下一句前面，看 a 后面紧跟的那个 token 是不是格助詞/係助詞。
    （Janome 有时会把 a 和助詞合成一个 token，比如「家の」，那也算。）
    """
    try:
        toks = list(_janome().tokenize(a + right_head))
    except Exception:                               # noqa: BLE001
        return False
    pos = 0
    for idx, t in enumerate(toks):
        end = pos + len(t.surface)
        if idx == 0 and end > len(a):               # a + 助詞 被合成了一个词（家の）
            return t.surface.startswith(a)
        if end == len(a):
            if idx + 1 >= len(toks):
                return False
            if _false_particle(toks, idx + 1):
                return False
            return _is_particle_like(toks[idx + 1].part_of_speech.split(","), 1)
        if end > len(a):
            return False                            # a 被切进了别的词里
        pos = end
    return False


def ffprobe_error(mkv):
    """ffprobe 失败的**原因**。解析时用的是 -v quiet（避免警告混进 JSON），
    但那样失败时只剩一个空 {} —— 所以失败后再用 -v error 跑一次，只取错误信息。"""
    _, err = common.run([common.FFPROBE, "-v", "error", "-show_streams", str(mkv)])
    return " ".join(err.split())[:200] or "（ffprobe 没给出原因）"


def probe_streams(mkv):
    """ffprobe 读 JSON，返回 (音频列表, 字幕列表)"""
    code, out = common.run([common.FFPROBE, "-v", "quiet", "-print_format", "json",
                     "-show_streams", str(mkv)])
    if code != 0:
        raise RuntimeError(f"ffprobe 失败: {ffprobe_error(mkv)}")
    data = json.loads(out)
    videos, audios, subs = [], [], []
    for s in data.get("streams", []):
        st = s.get("codec_type")
        if st == "video":
            videos.append(s)
        elif st == "audio":
            audios.append(s)
        elif st == "subtitle":
            subs.append(s)
    return audios, subs


def has_jp_subtitle(mkv):
    """该 mkv 是否已带 jpn(日语) 字幕轨"""
    try:
        _, subs = probe_streams(mkv)
    except Exception:
        return False
    for s in subs:
        lang = s.get("tags", {}).get("language", "")
        if lang.lower() in ("jpn", "ja"):
            return True
    return False


def pick_audio(mkv, audios):
    """优先选 jpn 标签音频轨，否则取第一条"""
    for a in audios:
        if a.get("tags", {}).get("language", "").lower() in ("jpn", "ja"):
            return a
    return audios[0] if audios else None


def extract_wav(mkv, audio_stream, wav_path):
    """指定音频轨转 16kHz 单声道 wav(faster-whisper 最合适输入)。
    audio_stream 是 ffprobe 返回的 dict，用其全局 stream index 做 -map 0:{index}。"""
    aidx = audio_stream.get("index")
    code, out = common.run([
        common.FFMPEG, "-y", "-i", str(mkv),
        "-map", f"0:{aidx}",
        "-vn", "-ac", "1", "-ar", "16000",
        str(wav_path),
    ])
    if code != 0:
        raise RuntimeError(f"抽音频失败: {out}")
    return wav_path


def model_path():
    """本地模型目录(手动下载的 large-v3 5 个文件都在此)。
    直接指向该目录，避免 faster-whisper 去 HuggingFace 缓存结构联网下载。"""
    return str(Path(common.MODEL_DIR) / "large-v3")


# whisper large-v3 需要的文件（缺一个都会加载失败）
# ⚠ 词表两种命名都存在：HuggingFace 上是 vocabulary.json，有些镜像站给的是 vocabulary.txt，
#   所以这里只要求"有其一"（踩过：只认 .txt 会把能正常跑的模型判成"缺文件"）。
MODEL_FILES = ("model.bin", "config.json", "tokenizer.json", "preprocessor_config.json")
MODEL_VOCAB = ("vocabulary.json", "vocabulary.txt")

# 听写/注音要用的 python 包（模块名 -> pip 包名）
REQUIRED_PY = (("faster_whisper", "faster-whisper"),
               ("janome", "Janome"),
               ("PIL", "pillow"))


def check_python_deps():
    """python 依赖装了没？返回给用户看的说明（None = 齐了）。
    依赖是懒加载的（不用就不 import），所以这里显式查一遍，免得跑到一半才
    蹦一句 No module named 'faster_whisper'。"""
    import importlib
    missing = []
    for mod, pkg in REQUIRED_PY:
        try:
            importlib.import_module(mod)
        except Exception:                               # noqa: BLE001
            missing.append(pkg)
    if not missing:
        return None
    return (f"缺少 Python 包：{'、'.join(missing)}\n"
            "     装法（在项目目录下）：\n"
            r"         .venv\Scripts\python.exe -m pip install -r requirements.txt" + "\n"
            f"     或者：.venv\\Scripts\\python.exe -m pip install {' '.join(missing)}")


# 模型：**不做自动下载**（用户定的：自己下、手动放进 models\，见 check_model 的提示）。
# ⚠ 写提示时别把仓库写错：openai/whisper-large-v3 是 Transformers 格式（model.safetensors
#   + vocab.json），**没有 model.bin**，faster-whisper 也读不了（实测 404 踩过）。要的是
#   CTranslate2 格式的 Systran/faster-whisper-large-v3，里面正好是下面这 5 个文件。
MODEL_REPO = "Systran/faster-whisper-large-v3"
MODEL_REPO_BAD = "openai/whisper-large-v3"      # 只用来在提示里点名"别下这个"
HF_MIRROR = "https://hf-mirror.com"            # 国内连不上 huggingface.co 时用的镜像


def check_model():
    """模型就位吗？返回给用户看的说明（None = 就位）。
    为什么要有这一步：模型是 3GB 的独立下载，不是 pip 依赖，而且**故意不做自动下载**
    （用户定的：自己下、手动放进 models\）。少了它 faster-whisper 会把这个路径当成模型名
    去 HuggingFace 下载——国内网络下要么卡死要么报一堆看不懂的错。这里提前拦住，
    直接告诉用户去哪儿下、下哪 5 个文件、放到哪个目录。"""
    d = Path(model_path())
    if not d.is_dir():
        return _model_help(f"找不到模型目录：{d}")
    missing = [f for f in MODEL_FILES if not (d / f).is_file()]
    if not any((d / v).is_file() for v in MODEL_VOCAB):
        missing.append(MODEL_VOCAB[0])
    if missing:
        return _model_help(f"模型目录 {d} 里缺文件：{'、'.join(missing)}"
                           f"（词表叫 vocabulary.json 或 vocabulary.txt 都认）")
    return None


def _model_help(reason):
    """模型没就位时给用户看的话：去哪儿下、下哪些、放到哪儿。"""
    return (
        f"{reason}\n"
        f"     whisper large-v3 模型要**自己下**（约 3GB，本工具不自动下），放进这个目录：\n"
        f"       {model_path()}\n"
        f"     需要这 5 个文件：{'、'.join(MODEL_FILES)} 和 vocabulary.json\n"
        f"     下载地址：\n"
        f"       原站：https://huggingface.co/{MODEL_REPO}/tree/main\n"
        f"       国内镜像：https://hf-mirror.com/{MODEL_REPO}/tree/main\n"
        f"     注意：仓库要用 CTranslate2 格式的 {MODEL_REPO}；\n"
        f"       {MODEL_REPO_BAD} 是 Transformers 格式，里面没有 model.bin，faster-whisper 用不了。\n"
        f"     3GB 建议用能续传的工具下，例如：aria2c -c -x16 -s16 -k2M <链接>"
        f"（在页面里点文件名就能拿到链接）\n"
        f"     想放到别处：config.ini 的 [paths] model_dir = 别的路径（见 README「模型文件」）"
    )


def pick_device():
    """选推理设备：有 CUDA 就用，没有就回退 CPU。
    （以前写死 cuda，没 N 卡的人直接崩——开源后这条不能留。）"""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", COMPUTE_TYPE
    except Exception:                                   # noqa: BLE001
        pass
    return "cpu", "int8"


# ===== 时间轴校验相关 =====

def extract_timeline_base(mkv):
    """提取中文字幕轨做断句基准，解析为 [(s,e),...]，返回 (list, max_end, lang)。
    按语言标签挑中文字幕轨，不盲取第一条——第一条可能是 signs / 英文 / 其他语言，
    拿它当基准会把整条时间轴带偏。找不到中文标签才退回第一条并告警。
    只用于断句/校验，不参与最终内封。"""
    try:
        _, subs = probe_streams(mkv)
    except Exception:
        return [], 0.0, ""
    if not subs:
        return [], 0.0, ""
    target = None
    for s in subs:
        if s.get("tags", {}).get("language", "").lower() in CHINESE_LANGS:
            target = s
            break
    if target is None:
        target = subs[0]
        fallback_lang = target.get("tags", {}).get("language", "") or "und"
        print(f"  [warn] 没有中文语言标签的字幕轨，退回第一条 "
              f"(language={fallback_lang})，断句基准可能不准")
    base_lang = target.get("tags", {}).get("language", "") or "und"
    idx = target.get("index")
    code, out = common.run([common.FFMPEG, "-v", "error", "-i", str(mkv), "-map", f"0:{idx}",
                     "-f", "srt", "-"])
    if code != 0 or not out:
        return [], 0.0, base_lang
    timeline = common.parse_srt_timeline(out)
    max_end = max((e for _, e in timeline), default=0.0)
    return timeline, max_end, base_lang


@lru_cache(maxsize=8192)
def _starts_sentence(t):
    """t 是不是"只会出现在句首"的词：いや・ええ・うん・まあ（感動詞），
    えーと・あの（フィラー），でも・だから（接続詞）。这种词不可能给上一句收尾。"""
    try:
        toks = list(_janome().tokenize(t))
    except Exception:                               # noqa: BLE001
        return False
    if len(toks) != 1 or toks[0].surface != t:
        return False
    return toks[0].part_of_speech.split(",")[0] in ("感動詞", "接続詞", "フィラー")


def _repair_boundaries(groups, ivs, debug=False):
    """边界修复：中文断句边界和日语的"词/短语"边界不重合时，上一条末尾那个碎片
    其实只是某个词的开头，要把它挪到下一句。两种形态：

      ① 词被切两半：  …思う|ス   +   ポーツ|…      → 挪「ス」
      ② 下一条从格助詞开头：…ですか|俺 + が|…        → 挪「俺」
      ③ 碎片跨在边界上、而且是个只会起句的词：…|いや + そんな|…  → 挪「いや」

    只在交界处动**一个**碎片；句尾是助詞/助動詞（だ・な・ね）时不动，
    中间有明显停顿时也不动。
    """
    for k in range(len(groups) - 1):
        i, wa = groups[k]
        j, wb = groups[k + 1]
        if not wa or not wb or i is None or j is None or i >= j:
            continue
        a, b = wa[-1], wb[0]
        gap = b[0] - a[1]
        cut = ivs[i][1]                     # 这一条中文的结束 = 下一条的开始
        right_head = "".join(w[2] for w in wb)[:10]
        word_cut = (a[0] <= cut <= a[1] + CUT_SLACK) and _joined_one_word(a[2], b[2])
        particle_cut = (a[0] <= cut <= b[1] + CUT_SLACK) \
            and _next_starts_with_particle(a[2], right_head)
        interjection_cut = (a[0] < cut < a[1]) and _starts_sentence(a[2])
        why = None
        if gap > GAP_WORD:
            why = f"中间有停顿(gap={gap:.2f})"
        elif _is_bound_tail(a[2]):
            why = "上一句句尾是助詞/助動詞"
        elif not (word_cut or particle_cut or interjection_cut):
            why = "三条规则都不满足"
        if debug:
            print(f"  [dbg] a={a[2]!r}[{a[0]:.2f}-{a[1]:.2f}] b={b[2]!r}[{b[0]:.2f}-{b[1]:.2f}] "
                  f"cut={cut:.2f} gap={gap:.2f} 词={word_cut} 助詞={particle_cut} "
                  f"起句词={interjection_cut} → {why or '挪'}")
        if why:
            continue
        wa.pop()
        wb.insert(0, a)


def align_timeline(segments, timeline, max_end=0.0, audio_dur=0.0):
    """按中文字幕的断句边界切分 Whisper 的输出（词级时间）。

    为什么不再"整段吸附"：Whisper 的断句和中文字幕(意译)的断句天然不一致。
      - Whisper 把两句并成一段 -> 整段只能挂到一条中文台词上，另一条彻底没字幕，
        挂上的那条挤成两行超长句(实测 10~18 字/秒，读不完)；
      - Whisper 把一句拆成两段 -> 同一中文区间被均分，同样挤。
    改成词级切分后：词中点落在哪条中文台词里就归哪条，同一组出一个条目。
      - 有主的词 -> 时间用该中文区间的 [cs, ce]（与中文字幕同进同出，便于对照）；
      - 落空的词 -> 保留 Whisper 的词级时间单独成条（不丢内容）。
    这样两条中文台词各自都有日语，且谁也不会被挤扁。

    segments: [(start, end, text, words), ...]，words = [(ws, we, w), ...]（可为空）
    timeline: [(cs, ce), ...] 中文字幕区间
    返回 [(start, end, text), ...]（已排序，已做上限保护）
    """
    GAP = 0.6        # 词落空时，距最近中文区间多远之内仍算它的

    # 1) 摊平成词序列；没有词级时间就退回"整段当一个词"
    words = []
    for seg in segments:
        s, e, text = seg[0], seg[1], seg[2]
        ws = seg[3] if len(seg) > 3 else None
        if ws:
            for a, b, w in ws:
                if b < a:
                    a, b = b, a
                words.append((float(a), max(float(b), float(a) + 0.05), w))
        elif text:
            words.append((float(s), max(float(e), float(s) + 0.05), text))
    if not words:
        return []

    ivs = sorted(timeline) if timeline else []

    def owner(mid):
        """词中点归属的中文区间下标；两边都不沾就返回 None(落空)。"""
        best_i, best_d = None, None
        for i, (cs, ce) in enumerate(ivs):          # 先找"包住"它的
            if cs <= mid <= ce:
                d = abs(mid - (cs + ce) / 2.0)
                if best_d is None or d < best_d:
                    best_i, best_d = i, d
        if best_i is not None:
            return best_i
        for i, (cs, ce) in enumerate(ivs):          # 再找时间上最近的
            d = (cs - mid) if mid < cs else (mid - ce)
            if best_d is None or d < best_d:
                best_i, best_d = i, d
        return best_i if (best_i is not None and best_d <= GAP) else None

    # 2) 相邻同归属的词并成一组
    groups = []
    for w in words:
        i = owner((w[0] + w[1]) / 2.0)
        if groups and groups[-1][0] == i:
            groups[-1][1].append(w)
        else:
            groups.append((i, [w]))

    # 2.5) 边界修复（见 _repair_boundaries）
    _repair_boundaries(groups, ivs)

    entries = []
    for i, ws in groups:
        text = "".join(w[2] for w in ws).strip()
        if not text:
            continue
        if i is not None and i < len(ivs):
            s, e = ivs[i]
        else:
            s, e = ws[0][0], ws[-1][1]
        if e <= s:
            e = s + 0.3
        entries.append((s, e, text))

    # 3) 同一中文区间被切成多个不相邻的组时合并成一条，避免同时间重复显示
    merged = {}
    for s, e, text in sorted(entries, key=lambda x: x[0]):
        key = (round(s, 3), round(e, 3))
        merged[key] = merged.get(key, "") + text
    entries = sorted([(s, e, t) for (s, e), t in merged.items()], key=lambda x: x[0])

    # 4) 上限保护：只防"时间轴爆表"，不再拿中文 max_end 砍合法台词
    cap = audio_dur if audio_dur else max_end
    if cap:
        entries = [(min(s, cap), min(e, cap), t) for (s, e, t) in entries]
    return entries


def qa_stats(entries, total_dur):
    """跑完自检指标：条目数 / 覆盖时长 / 语速异常条数(>10 字/秒，读不赢)。"""
    cram = sum(1 for s, e, t in entries if (e - s) > 0 and len(t) / (e - s) > 10)
    covered = sum(e - s for s, e, _ in entries)
    return {"n": len(entries), "covered": covered, "total": total_dur, "cram": cram}


def write_srt(seg_list, srt_path):
    """把 [(start, end, text), ...] 写成 srt。"""
    entries = []
    for k, (s, e, text) in enumerate(seg_list):
        if not text:
            continue
        entries.append(f"{k+1}\n{common.fmt_srt_ts(s)} --> {common.fmt_srt_ts(e)}\n{text}\n")
    srt_path.write_text("\n".join(entries), encoding="utf-8")
    return srt_path


def _cuda_runtime_error(exc):
    """这个报错像不像"CUDA 运行库缺失/加载不了"？
    典型：Library cublas64_12.dll is not found or cannot be loaded
    （只装显卡驱动、没装 CUDA 运行库的机器；clean 环境实测踩到）。"""
    msg = str(exc).lower()
    return any(k in msg for k in ("cublas", "cudnn", "cudart", "cuda"))


def _transcribe_once(device, compute_type, wav_path, max_end):
    """跑一遍听写（加载模型 + 消费完整个生成器），返回 (每段原始结果, 音频总长)。
    ⚠ 一定要在这里就把生成器消费掉：faster-whisper 的 transcribe() 返回的是**生成器**，
    CUDA 缺 DLL 那种错误是取第一条结果时才炸出来的，光 try 住 transcribe() 没用（踩过）。
    ⚠ VAD 参数走 VAD_PARAMS（min_silence 从默认 2000ms 缩到 250ms）：默认参数下说话
    中间的换气会被并进同一块，词级时间被块内 DTW 拉歪，断句就跟真停顿对不上了。"""
    from faster_whisper import WhisperModel
    from faster_whisper.vad import VadOptions
    print(f"  [转写] 加载模型 large-v3（{device} / {compute_type}）...", flush=True)
    model = WhisperModel(model_path(), device=device, compute_type=compute_type)
    segments, info = model.transcribe(
        str(wav_path), language=LANG, beam_size=BEAM_SIZE, vad_filter=VAD_FILTER,
        vad_parameters=dataclasses.asdict(VadOptions(**VAD_PARAMS)),
        word_timestamps=True, condition_on_previous_text=False,
    )
    total_dur = float(info.duration or 0.0) or float(max_end or 0.0)
    seg_list = []
    t_start = time.monotonic()
    last_print = 0.0
    print(f"  [转写] 开始，音频总长 {total_dur/60:.1f} 分钟...", flush=True)
    for seg in segments:
        st, en = float(seg.start), float(seg.end)
        words = [(float(w.start), float(w.end), w.word) for w in (seg.words or [])]
        seg_list.append((st, en, (seg.text or "").strip(), words))
        # 实时进度：每约5秒刷新一次
        now = time.monotonic()
        if now - last_print >= 5:
            pct = (en / total_dur * 100) if total_dur else 0.0
            print(f"  [转写] 已识别 {en/60:.1f}/{total_dur/60:.1f} 分钟 ({pct:.0f}%) "
                  f"耗时 {now-t_start:.0f}s，共 {len(seg_list)} 句", flush=True)
            last_print = now
    print(f"  [转写] 完成，共 {len(seg_list)} 句，用时 {time.monotonic()-t_start:.0f}s", flush=True)
    return seg_list, total_dur


# ===== 日语自己断句（不再依赖中文字幕轨） =====
# 以前拿中文轨的断句当基准：好处是"人类翻译的断句比 whisper 的段边界靠谱"，
# 代价是把日语切在翻译的断点上（半个词、行首助詞、感動詞跨行…）。用户只看日语轨，
# 所以改成"听日语自己的呼吸"：
#   silero VAD 实测停顿 → 停顿落到词缝上 → DP 按"单行宽度"拼行 → 时间用语音段做骨架
LINE_WIDTH_RATIO = 0.80      # 单行最多占画面宽度的比例（别占满屏，也绝不能折成两行）
HOLD_AFTER = 0.0             # 这句话说完后字幕再停留多久（秒）
                             # 用户 2026-09 定的：不用停留，说完就消失（觉得 1 秒太拖）。
                             # MIN_SHOW 仍然是下限，免得一闪而过看不清。
MIN_SHOW = 0.6               # 一行至少显示多久（秒）
PAUSE_CAND = 0.15            # 候选停顿：静音 >= 0.15s
PAUSE_STRONG = 0.30          # 强停顿：静音 >= 0.3s（优先在这儿断行）
LEAD_IN = 0.05               # 字幕比语音早一点点出现
SNAP_TOL = 0.60              # whisper 的段边界离停顿多远以内，就认它是同一个断点
                             # （实测 80% 在 0.3s 内、98% 在 0.6s 内）
INNER_PAUSE_MIN = 3.5        # 段内停顿只有当这一段词时长 >= 3.5s（一行肯定放不下）才算切点
_END_PUNCT = "。！？!?…"


def detect_pauses(wav_path, threshold=0.5, min_silence=PAUSE_CAND):
    """用 silero VAD（faster-whisper 自带，不用装新东西）从音频里找"换气/停顿"。
    返回 (语音段列表, 停顿列表)；拿不到就返回 ([], [])，调用方退回 whisper 词间间隔。
    语音段给"字幕什么时候出现/消失"当骨架，停顿给"在哪儿断行"当候选。"""
    try:
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import VadOptions, get_speech_timestamps
    except Exception:                               # noqa: BLE001
        return [], []
    try:
        audio = decode_audio(str(wav_path), sampling_rate=16000)
        opts = VadOptions(threshold=threshold,
                          min_silence_duration_ms=int(min_silence * 1000),
                          min_speech_duration_ms=100, speech_pad_ms=0)
        raw = get_speech_timestamps(audio, opts, sampling_rate=16000)
    except Exception as e:                          # noqa: BLE001
        print(f"  [warn] VAD 停顿检测失败（{e}），退回用 whisper 的词间间隔")
        return [], []
    spans = [(s["start"] / 16000.0, s["end"] / 16000.0) for s in raw]
    gaps = [(spans[i][1], spans[i + 1][0], spans[i + 1][0] - spans[i][1])
            for i in range(len(spans) - 1)]
    return spans, [g for g in gaps if g[2] > 0]


def _line_width(text, main_px):
    """这一行渲染出来有多宽（像素）。用字体文件的真实字宽算，不猜 1em/0.5em。"""
    if not HAVE_FURIGANA:
        return len(text) * main_px                  # 没字体度量时按全角估
    return furigana.text_em(text) * main_px


@lru_cache(maxsize=8192)
def _bad_line_start(text):
    """行首是不该起句的助詞吗（格助詞・係助詞・連体化の・準体助詞・副助詞）。"""
    if not text:
        return False
    try:
        toks = list(_janome().tokenize(text[:4]))
    except Exception:                               # noqa: BLE001
        return False
    if not toks:
        return False
    if _false_particle(toks, 0):
        return False
    return _is_particle_like(toks[0].part_of_speech.split(","), 1)


@lru_cache(maxsize=8192)
def _looks_like_line_end(text):
    """行尾像一句话的结束吗（句末标点 / 助動詞・動詞・形容詞・感動詞）。"""
    if not text:
        return False
    if text[-1] in _END_PUNCT:
        return True
    try:
        toks = list(_janome().tokenize(text[-6:]))
    except Exception:                               # noqa: BLE001
        return False
    if not toks:
        return False
    pos = toks[-1].part_of_speech.split(",")
    return pos[0] in ("助動詞", "動詞", "形容詞", "感動詞")


@lru_cache(maxsize=8192)
def _ends_with_sentence_starter(text):
    """这句是不是以"只会出现在句首"的词结尾（いや・あの・でも…）。

    这种词不可能给上一句收尾：实测「…興味ないのかなっていや」被断在「いや」后面，
    听感上「いや」是和下一句「そんなことないです」连着的。
    ⚠ 只取**最后一个 token**判断，不能拿尾巴去 _starts_sentence（那要求整串就是一个词，
    尾巴是「なっていや」时永远判 False，这个坑踩过）。
    """
    if not text:
        return False
    try:
        toks = list(_janome().tokenize(text[-8:]))
    except Exception:                               # noqa: BLE001
        return False
    if not toks:
        return False
    pos = toks[-1].part_of_speech.split(",")
    return pos[0] in ("感動詞", "接続詞", "フィラー")


@lru_cache(maxsize=8192)
def _attach_penalty(prev_tail, next_head):
    """切点右边紧跟着的是**附属語**吗？是的话该罚多少（0 = 不是附属語，可以断）。

    日语一个「文節」= 自立語 + 挂在后面的附属語（助詞 / 助動詞 / 接尾辞）。在附属語
    前面断行就是把文節切开了——用户最反感的那类毛病。实测：
      「いかが|ですか」（です=助動詞）、「あり|まして」（まし=助動詞）、
      「北|くん」（くん=名詞,接尾）都是这么断坏的。
    格助詞/係助詞/連体化/準体助詞/副助詞 那一类罚最重：句子根本不可能从它开头
    （`_NO_LINE_START_PARTICLES`）。
    """
    if not prev_tail or not next_head:
        return 0.0
    try:
        toks = list(_janome().tokenize(prev_tail + next_head))
    except Exception:                               # noqa: BLE001
        return 0.0
    cut, pos = len(prev_tail), 0
    for i, t in enumerate(toks):
        if pos >= cut:                              # 右边第一个 token 就是它
            if _false_particle(toks, i):
                return 0.0                          # 「という」被拆成的 と+いう，不是助詞
            parts = t.part_of_speech.split(",")
            if _is_particle_like(parts, 1):
                return 6.0                          # 句子不可能从格助詞/係助詞开头
            if parts[0] == "助詞":
                return 4.0
            if parts[0] == "助動詞" or (parts[0] == "名詞" and len(parts) > 1
                                        and parts[1] == "接尾"):
                return 4.0                          # 助動詞/接尾辞：文節还没说完
            return 0.0
        if pos + len(t.surface) > cut:
            # 切点落在这个 token **内部**：这是"劈词"，由 _cut_inside_word 负责罚，
            # 这里不能拿后面那个词来判断（实测会误报成"下一行从助詞开头"）。
            return 0.0
        pos += len(t.surface)
    return 0.0


@lru_cache(maxsize=8192)
def _cut_inside_word(prev_tail, next_head):
    """这个切点是不是切在"一个词"的内部（把两边拼起来分词，看有没有 token 跨过切点）。

    比"两截拼起来是不是一个词"更通用：`ゲーム機と考|えて` 这种也抓得到
    （Janome 会把 `考えて` 分成一个 token，跨过切点）。
    """
    if not prev_tail or not next_head:
        return False
    joined = prev_tail + next_head
    cut = len(prev_tail)
    try:
        toks = list(_janome().tokenize(joined))
    except Exception:                               # noqa: BLE001
        return False
    pos = 0
    for t in toks:
        end = pos + len(t.surface)
        if pos < cut < end:
            return True
        pos = end
    return False


def _cut_penalty(prev_text, next_text, strength, free, trusted=False):
    """在"这一行结束/下一行开始"之间断开的代价（越小越该断在这儿）。

    注意停顿是**负代价（奖励）**：不然动态规划会"能不断就不断"，把好几句话并成一行。
    free=True 表示这个切点前面根本没有上一行（整集第一行），不用判代价。
    trusted=True 表示这是 **whisper 自己标的句界**（`_retime_words` 已经把它修到语言上
    合法的位置）：这时**不再**拿"16 个字的碎片"去问 Janome "下一行是不是助詞开头"——
    那套在碎片上下文里会误报（实测 31 处拦截里 20 处是误报，用户看到的就是
    "开头词被并进上一句"）。

    权重的相对大小是有意的（实测调过）：
      * 「切在一个词内部」+30 是**压倒性**的——宁可这行宽一点，也不能把 スポーツ 切成
        「ス」「ポーツ」（用户最容易一眼看出来的毛病）。
      * 「行尾是いや/でも/あの」+6、「下一行从格助詞开头」+6：这两条是**语法硬规则**，
        必须大过停顿奖励（-1.5），否则动态规划为了吃停顿奖励照样把「いや」甩在行尾
        （实测就是它把「…かなっていや」断在「いや」后面）。
    """
    if free:
        return 0.0
    if strength >= PAUSE_STRONG:
        cost = -1.5                         # 强换气 → 就该在这儿断
    elif strength >= PAUSE_CAND:
        cost = -0.5                         # 弱换气 → 可以断
    else:
        cost = 1.0                          # 没有换气 → 尽量别断
    if prev_text and next_text:
        if _cut_inside_word(prev_text[-6:], next_text[:8]):
            cost += 30.0                    # 切在词内部 → 基本禁止
        if _ends_with_sentence_starter(prev_text):
            cost += 6.0                     # 感動詞/接続詞 只会起句，不该留在行尾
        if not trusted:
            cost += _attach_penalty(prev_text[-10:], next_text[:16])
            # 下一行从附属語（助詞/助動詞/接尾）开头 → 把一个文節切开了
    if not _looks_like_line_end(prev_text):
        cost += 0.5                         # 行尾不像句末（弱惩罚）
    return cost


def _flatten_words(segments):
    """把 whisper 的段+词摊平成一个按时间排序的词序列（没有词级时间就整段当一个词）。"""
    words = []
    for seg in segments:
        s, e, text = seg[0], seg[1], seg[2]
        ws = seg[3] if len(seg) > 3 else None
        if ws:
            for a, b, w in ws:
                if b < a:
                    a, b = b, a
                words.append((float(a), max(float(b), float(a) + 0.05), w))
        elif text:
            words.append((float(s), max(float(e), float(s) + 0.05), text))
    words.sort(key=lambda x: x[0])
    return words


def _seg_words(seg):
    """取出这一段里的词（没有词级时间就拿整段当一个词）。"""
    ws = seg[3] if len(seg) > 3 else None
    if ws:
        return [(float(a), max(float(b), float(a) + 0.05), w) for a, b, w in ws]
    s, e, t = float(seg[0]), float(seg[1]), seg[2]
    return [(s, max(e, s + 0.05), t)]


def _nearest_pause(t, pauses, tol):
    """离 t 最近的那条停顿（t 落在停顿区间里就算 0 距离）；超过 tol 就当没有。"""
    best, hit = tol, None
    for pa, pb, gap in pauses:
        d = 0.0 if pa <= t <= pb else min(abs(t - pa), abs(t - pb))
        if d <= best:
            best, hit = d, (pa, pb, gap)
    return hit


def _segment_ranges(segments, spans, pauses, tol=SNAP_TOL):
    """算出 whisper 每个"段"真实的起止时间（返回 [(start, end), ...]）。

    为什么按**段**来对齐（而不是按词）：
      * whisper 的"句子切分"是可信的——实测它把「何かの友達を作ろうとして」/「俺は自分自身の
        問題に気づいてしまった」/「タ君ですよね」正好切开，用户要的断点就在这些地方；
      * 不可信的只是它的**时间**：段边界平均离真停顿 0.21s（80% 在 0.3s 内、98% 在 0.6s 内），
        而段首词的时间会被拉到段边界上（实测「俺」差 0.5s、「タ」差 4.2s——照词级时间对齐，
        "该断的地方"就变成了"词中间"，用户实测一眼就看出来了）。
    做法：把段边界吸附到最近的 VAD 停顿上——停顿的**起点**就是上一句的结束、**终点**就是
    这一句的开始；连说话中间没停（附近没有停顿）就保持原样。
    """
    n = len(segments)
    if not n:
        return []
    starts = [float(segments[0][0])] + [0.0] * (n - 1)
    ends = [0.0] * (n - 1) + [float(segments[-1][1])]
    if spans and abs(starts[0] - spans[0][0]) <= tol:
        starts[0] = spans[0][0]
    if spans and abs(ends[-1] - spans[-1][1]) <= tol:
        ends[-1] = spans[-1][1]
    for i in range(n - 1):
        b = (float(segments[i][1]) + float(segments[i + 1][0])) / 2.0
        hit = _nearest_pause(b, pauses, tol)
        if hit:
            ends[i], starts[i + 1] = hit[0], hit[1]
        else:
            ends[i] = starts[i + 1] = b
    # 收尾：吸附可能把两条边界吸到同一条停顿上，中间那段就被挤成 0 长度甚至倒过来。
    # 这里统一保证"起点不回退、结束不早于开始"（实测不夹的话产物里会出现负时长的行）。
    prev_end = None
    for i in range(n):
        s = starts[i] if prev_end is None else max(starts[i], prev_end)
        e = max(ends[i], s)
        starts[i], ends[i], prev_end = s, e, e
    return list(zip(starts, ends))


def _mid_word_shift(prev_text, next_head):
    """whisper 的段边界切在一个词内部吗（它把「将来」切成 将｜来）？

    是的话返回**那个词的起点**在上一段里的字符位置（0 = 没切在词里）。
    这样调用方可以把切点往左挪到词首，让整个词归到下一句，而不是把它劈成两半
    （老的中文基准路径里有一条同名的修复，日语路径重做时漏了）。
    """
    if not prev_text:
        return 0
    try:
        toks = list(_janome().tokenize(prev_text + (next_head or "")))
    except Exception:                                  # noqa: BLE001
        return 0
    cut, pos = len(prev_text), 0
    for t in toks:
        start, end = pos, pos + len(t.surface)
        if start < cut < end:                          # 切点在这个词内部
            return start
        if end >= cut:
            break
        pos = end
    return 0


def _is_starter_word(word):
    """这个词单独看是不是"只会起句"的词（いや・え・はい・でも…）。

    用**单独一个词**去问 Janome，而不是拿上下文碎片——实测碎片里 Janome 会把
    「はいいつき」切成 `は+いい+つき`（其实是「はい、いつきさん」），单独看「はい」就对了。
    """
    if not word or len(word) > 3:
        return False
    try:
        toks = list(_janome().tokenize(word))
    except Exception:                                  # noqa: BLE001
        return False
    if len(toks) != 1 or toks[0].surface != word:
        return False
    return toks[0].part_of_speech.split(",")[0] in ("感動詞", "接続詞", "フィラー")


def _trailing_starter_start(text):
    """这一段的**末尾**是一串"只会起句"的词吗（え・はい・ああ・いや…）？

    返回这串的起点字符位置，否则 0。实测：whisper 常把下一句开头的「え」「はい」留在
    上一段末尾，我们的规则又"不许行尾是感動詞"，结果只能整段并进上一行——于是用户看到
    "开头词被吞进上一句"。正确做法是把这串挪到下一句。

    ⚠ 整段都是起句词时（`start <= 0`）不动：那本来就是独立的一句（「はい」自己成行）。
    ⚠ 必须拿**整段文本**去分词：whisper 的词是 BPE 碎片，单看「う」「え」会把「思う」
    的尾巴当成感動詞（实测踩到，会把「…損はないと思う」切坏）。
    """
    if not text:
        return 0
    try:
        toks = list(_janome().tokenize(text))
    except Exception:                                  # noqa: BLE001
        return 0
    pos, start_at = len(text), 0
    for t in reversed(toks):
        start = pos - len(t.surface)
        parts = t.part_of_speech.split(",")
        if start > 0 and parts[0] in ("感動詞", "接続詞", "フィラー"):
            start_at, pos = start, start
        else:
            break
    return start_at


def _leading_attach_len(text, max_tokens=2):
    """这一段的**开头**是不能当句首的附属語吗（の・が・を・に・だ…）？

    返回应该"归到上一句"的字符数（0 = 不用动）。实测：whisper 的段边界偶尔早了一个词
    （`…それがでいい｜のよし2人とも…`、`…変だったから私｜だったって…`），把开头的
    「の」「だ」并回上一句才对。
    """
    if not text:
        return 0
    try:
        toks = list(_janome().tokenize(text))
    except Exception:                                  # noqa: BLE001
        return 0
    acc, used = 0, 0
    for t in toks:
        if used >= max_tokens:
            break
        parts = t.part_of_speech.split(",")
        if _is_particle_like(parts, 1) or parts[0] == "助動詞" or (
                parts[0] == "名詞" and len(parts) > 1 and parts[1] == "接尾"):
            acc += len(t.surface)
            used += 1
        else:
            break
    if acc >= len(text):                               # 整段都是附属語 → 不动
        return 0
    return acc


def _word_at_char(group, pos):
    """给定"这一段的第几个字符"，返回它落在第几个词上（0-based）；越界返回 None。"""
    acc = 0
    for j, (_a, _b, w) in enumerate(group):
        if pos < acc + len(w):
            return j
        acc += len(w)
    return None


def _starts_with_attach(text):
    """这一句**自己**的开头能不能当句首？（用整句判，不用切点附近的碎片）

    格助詞/係助詞/連体化/準体助詞/副助詞 + 助動詞 + 接尾 都算"不能当句首"
    （句子不可能从「が」「を」「です」「くん」开头）。
    ⚠ 只认这几类：終助詞（なあ）、接続助詞（とも）、感動詞（はい）都**可以**当句首，
    实测「はい」「でも」「だが」「なあ」这些在碎片里会被 Janome 拆错，拿整句判就对了。
    """
    if not text:
        return False
    try:
        toks = list(_janome().tokenize(text))
    except Exception:                                  # noqa: BLE001
        return False
    if not toks:
        return False
    if _false_particle(toks, 0):
        return False                    # 「というか」的 と 是引用助詞，不是真助詞
    parts = toks[0].part_of_speech.split(",")
    if _is_particle_like(parts, 1):
        return True
    if parts[0] == "助動詞":
        return True
    return parts[0] == "名詞" and len(parts) > 1 and parts[1] == "接尾"


def _retime_words(segments, spans, pauses):
    """把 whisper 的段/词贴回**真实音频时间轴**。

    返回 (新词序列, {切点下标: 停顿长度}, {可信句界下标})。

    段的起止时间见 `_segment_ranges`（边界吸附到 VAD 停顿上）；段内的词再按原时长比例
    铺开。⚠ 拉伸比例夹在 0.75~1.6 之间：段里混音乐/长静音时不许把一句 16 个字显示成 8 秒。

    "可信句界"= whisper 标的句子边界，并且已经修到语言上合法的位置（切在词内部 → 挪到
    词首；末尾是「え/はい」这种只会起句的词 → 挪到下一句；开头是「の/だ」这种不能起句的
    附属語 → 并回上一句）。拼行的动态规划对这些下标不再做"是不是助詞起句"的二次猜疑。
    """
    if not segments:
        return [], {}, set()
    words = _flatten_words(segments)
    if not words:
        return [], {}, set()
    if not spans:
        cuts = {k: words[k][0] - words[k - 1][1] for k in range(1, len(words))
                if words[k][0] - words[k - 1][1] >= PAUSE_CAND}
        return words, cuts, set()

    out, cuts, trusted = [], {}, set()
    prev_end = None
    for i, (seg, (s0, e0)) in enumerate(zip(segments, _segment_ranges(segments, spans, pauses))):
        group = _seg_words(seg)
        s = s0 if prev_end is None else max(s0, prev_end)   # 时间不许倒流
        total = sum(b - a for a, b, _ in group) or 1.0
        scale = min(1.6, max(0.75, max(0.05, e0 - s0) / total))
        idx0 = len(out)
        t = s
        for a, b, w in group:
            d = scale * max(0.05, b - a)
            out.append((t, t + d, w))
            t += d
        # 段与段之间的切点：先取"音频实测的停顿"，再把切点**修到语言上合法的位置**——
        # ① 切在词内部的（whisper 把「将来」切成 将｜来）→ 挪到词首，整词归下一句；
        # ② 上一段末尾是"只会起句"的词（え・はい・ああ…其实是下一句的开头）→ 挪到下一句。
        # 不修的话只能整段并进上一行，就是用户说的"开头词被切到上一句里面了"。
        if i:
            pause = 0.0
            if s0 > prev_end:
                pause = s0 - prev_end                      # 段与段之间的真停顿
            else:
                # 吸附有时把两条边界吸到同一条停顿上（中间那段被挤成 0 长度），几何差
                # 算不出停顿；直接查那条停顿本身，别丢掉这个切点。
                hit = _nearest_pause((float(segments[i - 1][1]) + float(segments[i][0])) / 2.0,
                                     pauses, SNAP_TOL)
                if hit:
                    pause = hit[2]
            if pause > 0:
                prev_group = _seg_words(segments[i - 1])
                prev_text = segments[i - 1][2] or "".join(w for _a, _b, w in prev_group)
                cur_text = segments[i][2] or "".join(w for _a, _b, w in group)
                shift = (_mid_word_shift(prev_text, cur_text)
                         or _trailing_starter_start(prev_text))
                idx = idx0
                if shift and prev_group:                 # 往左挪：这句的词归下一句
                    base = idx0 - len(prev_group)
                    j0 = _word_at_char(prev_group, shift)
                    if j0 is not None:
                        idx = base + j0
                    # 挪到词首后**再验一次**：whisper 的词边界和 Janome 的 token 边界不一定
                    # 重合，还在词内部就继续往左挪，直到干净或挪到这一段开头。
                    while idx > base:
                        left = "".join(w for _a, _b, w in prev_group[:idx - base])
                        if not _cut_inside_word(left[-6:], cur_text[:8]):
                            break
                        idx -= 1
                else:                                    # 往右挪：下一段开头的附属語并回来
                    lead = _leading_attach_len(cur_text)
                    if lead:
                        j1 = _word_at_char(group, lead)
                        if j1:
                            idx = idx0 + j1
                if 0 < idx <= idx0 + len(group):
                    # 切点右边那一句**自己**的开头不能当句首（が/を/です…）→ 这个句界
                    # 本身不合法：不发奖励、也不标成可信（拼行时自然并回去）。
                    if idx != idx0 or not _starts_with_attach(cur_text):
                        # 可信句界至少按"弱停顿"算：whisper 说这里断句、哪怕音频里没换气，
                        # 也比把下一句的开头并进上一行强（实测这样又能多救回几处）。
                        cuts[idx] = max(cuts.get(idx, 0.0), pause, PAUSE_CAND)
                        trusted.add(idx)
        # 段内还有停顿的（模型把好几句话并成了一段）：那些词缝也当停顿候选，这样"放不下
        # 必须拆行"时会优先拆在换气处。但只有这一段的词时长 >= INNER_PAUSE_MIN 才这么做——
        # 短段本来就是一句话，硬拆会变成「俺は」+「自分」这种碎片（实测踩到）。
        # 每条停顿只认**离它中心最近的那一个**词缝，否则重铺后有几个缝都落在停顿里，
        # 会被切好几刀。
        for pa, pb, gap in pauses if total >= INNER_PAUSE_MIN else ():
            best, arg = 1e9, None
            for k in range(max(idx0 + 1, 1), len(out)):
                t = out[k][0]
                if not (pa - 0.05 <= t <= pb + 0.05):
                    continue
                d = abs(t - (pa + pb) / 2.0)
                if d < best:
                    best, arg = d, k
            if arg is not None:
                cuts.setdefault(arg, gap)
        prev_end = t
    return out, cuts, trusted


def segment_lines(segments, spans, pauses, width=None, height=None, main_px=None):
    """日语自己断句：把 whisper 词序列切成"一行一句"，返回 [(start, end, text)]。

    断行候选 = whisper 自己的**段边界**（= 模型认为的句界，落在哪条词缝上由 `_retime_words`
    给出来，缝的宽度就是音频实测的停顿）+ 段内所有词缝（代价高，只在放不下时才用）；
    用动态规划一次选一组切点，目标：
      * 每行都放得下（宽度 <= 画面 80%，按真实字宽算，保证注音层单行）
      * 行尾尽量落在强停顿（换气）上
      * 不把一个词切开 / 下一行不从助詞开头 / 感動詞不留在行尾
    显示时间用**语音段**做骨架：语音起点（提前 LEAD_IN）即出现，这句话说完就消失
    （HOLD_AFTER = 0，用户 2026-09 定的：不停留）；MIN_SHOW 是下限，免得一闪而过看不清，
    而且不超过下一句的开始。
    """
    words = _flatten_words(segments)
    if not words:
        return []
    if not main_px:
        main_px = max(20, int(round((height or 1080) * (60 / 1080.0))))
    limit = max(1.0, (width or 1920) * LINE_WIDTH_RATIO)

    # whisper 的词级时间不可靠（段首词会被拉到段边界上、段自身的起止还把静音算进去），
    # 用 VAD 语音段把每个"段"贴回真实音频时间轴：段内按原时长比例重铺，段与段之间就是真停顿。
    # 没有 VAD 数据时 _retime_words 会退回用 whisper 自己的词间间隔（不够准，但比没有强）。
    words, strength, trusted = _retime_words(segments, spans, pauses)

    n = len(words)
    texts = [w[2] for w in words]
    # 前缀宽度：width(i, j) = pref[j] - pref[i]（O(1)，不用每次拼字符串再量）
    pref = [0.0] * (n + 1)
    for k, t in enumerate(texts):
        pref[k + 1] = pref[k] + _line_width(t, main_px)
    INF = float("inf")
    dp = [INF] * (n + 1)
    back = [0] * (n + 1)
    dp[0] = 0.0
    for j in range(1, n + 1):
        for i in range(j - 1, -1, -1):
            if dp[i] == INF:
                continue
            width_px = pref[j] - pref[i]
            if width_px > limit and i < j - 1:
                break                       # 再往前加词只会更宽；单个词超宽只能放行
            if words[j - 1][1] - words[i][0] > 12.0:
                break                       # 一行不超过 12 秒
            text = "".join(texts[i:j])
            cost = dp[i] + _cut_penalty("".join(texts[max(0, i - 12):i]), text,
                                        strength.get(i, 0.0), i == 0, i in trusted)
            if i > 0 and _bad_line_start(text):
                cost += 4.0     # 下一行从格助詞/副助詞开头 → 句子不能这么起（要压过停顿奖励）
            dur = words[j - 1][1] - words[i][0]
            if dur < 0.4:
                cost += 2.0                 # 一两个词的"闪一下"：宁可丢掉停顿奖励也别留
            elif dur < 0.7:
                cost += 0.5
            elif dur > 8.0:
                cost += 0.5 * (dur - 8.0)   # 一行太长读不完（12 秒是硬上限）
            if cost < dp[j]:
                dp[j], back[j] = cost, i

    # 回溯出每行的词区间
    cuts = []
    j = n
    while j > 0:
        i = back[j]
        cuts.append((i, j))
        j = i
    cuts.reverse()
    if not cuts:
        return []

    entries = []
    for i, j in cuts:
        # 时间直接用**重铺后的词时间**：同一个语音段里可能切出两行，两行各用自己第一个
        # 词的起点。以前统一取"语音段起点"，同段两行会拿到同一个开始时间，前一行被压成
        # 0 秒甚至负数（实测 11 处 "结束<=开始"）。
        start = max(0.0, words[i][0] - LEAD_IN)
        end = max(start + MIN_SHOW, words[j - 1][1] + HOLD_AFTER)
        entries.append([start, end, "".join(texts[i:j]).strip()])
    for k in range(len(entries) - 1):            # 下一句来了就切，两行之间留 0.05s 缝
        nxt = entries[k + 1][0]
        entries[k][1] = min(entries[k][1], nxt - 0.05)
        entries[k][1] = max(entries[k][1], min(entries[k][0] + MIN_SHOW, nxt - 0.05))
    entries[-1][1] = max(entries[-1][1], entries[-1][0] + MIN_SHOW)
    return [(s, e, t) for s, e, t in entries if t]


def transcribe(wav_path, timeline, max_end, video=None, main_px=None, mode="jp"):
    """faster-whisper large-v3 听写，返回对齐好的 [(start, end, text), ...]。

    word_timestamps=True：只有拿到词级时间，才能把 Whisper"两句并一段"的输出
    重新切回两条中文台词的边界上。
    condition_on_previous_text=False：音乐/OP 段容易触发"重复上一句"的幻觉滚雪球
    (实测关掉 VAD 时会连出 5 条「ご視聴ありがとうございました」)，关掉这个开关
    可以让幻觉不再往下传。
    ⚠ CUDA 起不来（缺 cublas/cudnn DLL）时**自动改用 CPU 重跑**：以前只在加载前
    `get_cuda_device_count()` 判断，装了显卡驱动但没装 CUDA 运行库的机器会通过这个检查、
    然后在推理时炸掉（clean 环境实测）。

    断句两种模式（config.ini 的 [align] mode，默认 jp）：
      jp  日语自己断句：VAD 停顿 + DP（单行宽度）+ 语音时间骨架（用户只看日语轨）
      cn  老路径：按中文字幕轨的断句边界切（保留，可一键切回）
    """
    device, compute_type = pick_device()
    if device == "cpu":
        print("  [转写] 没检测到可用的 CUDA，回退到 CPU（会慢很多，1 集可能要几十分钟）",
              flush=True)
    while True:
        try:
            seg_list, total_dur = _transcribe_once(device, compute_type, wav_path, max_end)
            break
        except Exception as e:                          # noqa: BLE001
            if device == "cuda" and _cuda_runtime_error(e):
                print(f"  [!] CUDA 跑不起来：{e}\n"
                      f"      → 自动改用 CPU 重跑（慢很多）。想用上显卡："
                      f"把 cublas64_12.dll / cublasLt64_12.dll 放进 "
                      f"ctranslate2\\ 目录（见 README「N 卡用户注意」）", flush=True)
                device, compute_type = "cpu", "int8"
                continue
            raise

    if mode == "cn":
        # 老路径：按中文字幕的断句边界切（config.ini 里 [align] mode = cn 可以切回来）
        seg_list = align_timeline(seg_list, timeline, max_end, total_dur)
    else:
        # 新路径：日语自己断句——VAD 停顿当换气点，DP 按单行宽度拼行，时间跟语音走
        spans, pauses = detect_pauses(wav_path)
        if spans:
            print(f"  [断句] VAD: 语音 {len(spans)} 段 / 停顿 {len(pauses)} 处", flush=True)
        else:
            print("  [断句] 拿不到 VAD 停顿，退回用 whisper 词间间隔", flush=True)
        seg_list = segment_lines(seg_list, spans, pauses,
                                 width=video[0] if video else None,
                                 height=video[1] if video else None,
                                 main_px=main_px)
    return seg_list, total_dur, qa_stats(seg_list, total_dur)


def mkvmerge_subtitle_ids(mkv):
    """mkvmerge --identify 里所有字幕轨的 mkvmerge 轨道 ID(0-based)，按出现顺序。
    必须加 --ui-language en：mkvmerge 中文(GBK)输出在 common.run() 的 utf-8 解码下会乱码，
    导致 'subtitles' 匹配不到；强制英文后输出稳定为纯 ASCII，解析可靠。"""
    code, out = common.run([common.MKVMERGE, "--ui-language", "en", "--identify", str(mkv)])
    ids = []
    for line in out.splitlines():
        m = re.match(r"^Track ID (\d+): subtitles", line.strip())
        if m:
            ids.append(int(m.group(1)))
    return ids


def mkvmerge_tracks(mkv):
    """mkvmerge --identify 返回 [(轨道ID, 类型)]，只看 video/audio/subtitles(不含 attachment)。
    用于构造 --track-order。"""
    code, out = common.run([common.MKVMERGE, "--ui-language", "en", "--identify", str(mkv)])
    tracks = []
    for line in out.splitlines():
        m = re.match(r"^Track ID (\d+): (video|audio|subtitles)", line.strip())
        if m:
            tracks.append((int(m.group(1)), m.group(2)))
    return tracks


def find_first_track_by_lang(mkv, langs):
    """返回第一个语言匹配的字幕轨的 mkvmerge Track ID；找不到返回 None。
    probe_streams 返回的字幕顺序 与 mkvmerge_subtitle_ids 顺序一致(都按容器内轨道顺序)。"""
    sub_ids = mkvmerge_subtitle_ids(mkv)
    _, subs = probe_streams(mkv)
    for i, s in enumerate(subs):
        lang = s.get("tags", {}).get("language", "").lower()
        if lang in langs and i < len(sub_ids):
            return sub_ids[i]
    return None


def filter_subtitles(mkv):
    """内封后过滤：只保留第一条 jpn 与第一条 chi 字幕轨，其他所有语言字幕轨删除。
    用 mkvmerge -s(按轨道号保留，且必须放在输入文件之前才能作用于该文件)。
    注意 --track-order 只重排顺序、不删轨；真正删轨靠 -s。"""
    jpn_id = find_first_track_by_lang(mkv, ("jpn", "ja"))
    chi_id = find_first_track_by_lang(mkv, CHINESE_LANGS)
    keep = [str(x) for x in (jpn_id, chi_id) if x is not None]
    if not keep:
        return mkv
    out_mkv = str(Path(mkv).with_name(Path(mkv).stem + ".jpc.mkv"))
    code, out = common.run([common.MKVMERGE, "-o", out_mkv, "-s", ",".join(keep), str(mkv)])
    if code != 0:
        raise RuntimeError(f"过滤字幕失败: {out}")
    os.replace(out_mkv, mkv)
    return mkv


def tag_new_subtitle(mkv, mkv_id):
    """给指定字幕轨设 jpn 语言 + Japanese 轨道名 + 默认轨。
    设成默认：用户只看日语轨，让播放器一开就选中它，不用手动切。
    mkvpropedit 的 track:NN 从 1 起算，= mkvmerge --identify 的轨道 ID + 1。"""
    propedit_track = mkv_id + 1
    code, out = common.run([
        common.MKVPROPEDIT, str(mkv),
        "--edit", f"track:{propedit_track}",
        "--set", "language=jpn",
        "--set", "name=Japanese",
        "--set", "flag-default=yes",
    ])
    if code != 0:
        raise RuntimeError(f"mkvpropedit 打标签失败: {out}")


def mux_subtitle(mkv, srt_path, attach_font=None):
    """mkvmerge 流拷贝内封 srt 为日语轨(排到所有字幕轨最前)，打 jpn+Japanese 标签，
    再 filter_subtitles 只留 第一日语+第一汉语，删掉其他所有语言字幕。视频音频零重编码，附件全保留。
    attach_font: 要内附进 mkv 的字体文件（观众没装字体也能正确渲染注音）。
    之所以内封和过滤分两步：--track-order 只重排顺序、不过滤，真正删轨靠 mkvmerge -s。"""
    out_mkv = str(Path(mkv).with_name(Path(mkv).stem + ".jp.mkv"))
    tracks = mkvmerge_tracks(mkv)
    non_sub = [tid for tid, t in tracks if t != "subtitles"]
    subs = [tid for tid, t in tracks if t == "subtitles"]
    # --track-order: 输入0=原mkv, 输入1=srt；只用于把 jpn(1:0) 排到所有字幕最前。
    track_order = ",".join(f"0:{tid}" for tid in non_sub) + ",1:0"
    if subs:
        track_order += "," + ",".join(f"0:{tid}" for tid in subs)
    cmd = [common.MKVMERGE, "-o", out_mkv]
    # 字体内附：已经有同名附件就不重复加（重跑时)
    attached = 0
    if attach_font and Path(attach_font).is_file():
        name = Path(attach_font).name
        already = any((s.get("tags", {}).get("filename") or "") == name
                      for s in probe_json(mkv).get("streams", [])
                      if s.get("codec_type") == "attachment")
        if not already:
            cmd += ["--attach-file", str(attach_font)]
            attached = 1
            print(f"  内附字体: {name}")
        else:
            print(f"  字体已在文件里，跳过内附: {name}")
    cmd += [str(mkv), str(srt_path), "--track-order", track_order]
    code, out = common.run(cmd)
    if code != 0:
        raise RuntimeError(f"mkvmerge 内封失败: {out}")
    # 定位日语轨：track-order 把它排在所有字幕轨最前 = 识别出的第一条 subtitles。
    ids = mkvmerge_subtitle_ids(out_mkv)
    if not ids:
        raise RuntimeError(f"内封后未找到字幕轨: {out}")
    tag_new_subtitle(out_mkv, ids[0])
    filter_subtitles(out_mkv)   # 只留第一日语 + 第一汉语
    # 中文轨取消默认：日语已经是默认轨，别让播放器挑成中文
    chi_id = find_first_track_by_lang(out_mkv, CHINESE_LANGS)
    if chi_id is not None:
        common.run([common.MKVPROPEDIT, str(out_mkv), "--edit", f"track:{chi_id + 1}",
             "--set", "flag-default=no"])
    return out_mkv, attached


def replace_original(mkv, new_mkv):
    """stream copy 生成的 mkv 直接替换原文件(不备份)。视频音频一帧不动，只加字幕轨。"""
    os.replace(new_mkv, mkv)


def probe_json(mkv):
    """ffprobe --show_streams 的原始 JSON(自检用)。"""
    code, out = common.run([common.FFPROBE, "-v", "quiet", "-print_format", "json",
                     "-show_streams", str(mkv)])
    if code != 0:
        raise RuntimeError(f"ffprobe 失败: {ffprobe_error(mkv)}")
    return json.loads(out)


def media_signature(streams):
    """从 ffprobe 的 streams 里取出"必须保持不变"的部分：视频/音频的 codec + 附件文件名。

    ⚠ **不要比对附件的 codec_name**：mkvmerge 重新封装后，同样一个字体附件在 ffprobe
    眼里可能从 `ttf`/`otf` 变成 `None`（用户实测某集 10 个字体附件全中），拿它比对会
    误判成"视频/音频轨发生变化"而拒绝替换原文件。附件只比**数量**和**文件名**。
    """
    av = [(s.get("codec_type"), s.get("codec_name"))
          for s in streams if s.get("codec_type") in ("video", "audio")]
    atts = sorted((s.get("tags", {}).get("filename") or "")
                  for s in streams if s.get("codec_type") == "attachment")
    return av, atts


def video_size(mkv):
    """片源分辨率。注音 ASS 的 PlayRes 按它生成，换分辨率/换屏幕都不会错位。"""
    for s in probe_json(mkv).get("streams", []):
        if s.get("codec_type") == "video":
            return int(s.get("width") or 1920), int(s.get("height") or 1080)
    return 1920, 1080


def verify_muxed(orig_mkv, new_mkv, extra_attachments=0):
    """内封后独立复核(mkvmerge 自报 "muxed ok" 不算数)。返回问题列表，空=通过。

    检查项：新轨 language=jpn + title=Japanese、排在所有字幕轨最前、
    字幕只剩 日语+中文 两条、视频/音频轨与原件同 codec(证明确实 stream copy)。
    extra_attachments：这次内封额外加进去的附件数（内附字体时为 1），用于附件数量比对。
    """
    problems = []
    old, new = probe_json(orig_mkv), probe_json(new_mkv)

    def subs(d):
        return [s for s in d.get("streams", []) if s.get("codec_type") == "subtitle"]

    ns = subs(new)
    if not ns:
        problems.append("内封后没有字幕轨")
    else:
        first = ns[0]
        lang = (first.get("tags", {}).get("language") or "").lower()
        title = first.get("tags", {}).get("title") or ""
        if lang not in ("jpn", "ja"):
            problems.append(f"第一条字幕轨不是日语(language={lang or 'und'})")
        if title != "Japanese":
            problems.append(f"第一条字幕轨名不是 Japanese(title={title or '空'})")
        if len(ns) > 2:
            problems.append(f"字幕轨还剩 {len(ns)} 条，应只留 第一日语+第一中文")
        if not any((s.get("tags", {}).get("language") or "").lower() in ("jpn", "ja")
                   for s in ns):
            problems.append("没有 language=jpn 的字幕轨")
    old_av, old_atts = media_signature(old.get("streams", []))
    new_av, new_atts = media_signature(new.get("streams", []))
    if old_av != new_av:
        problems.append(f"视频/音频轨发生变化: {old_av} -> {new_av}")
    if len(new_atts) != len(old_atts) + extra_attachments:
        problems.append(f"附件数量不对: 原 {len(old_atts)} + 新增 {extra_attachments} "
                        f"应={len(old_atts) + extra_attachments}，实际 {len(new_atts)}")
    return problems


def collect_mkvs(target):
    """扫描目标，返回 (全部 mkv, 需要处理的, 已有日语轨的条数)。
    判定只看"有没有日语字幕轨"（内容判定，天然幂等，不受下载时间影响）。"""
    target = Path(target)
    if target.is_file() and target.suffix.lower() in (".mkv", ".mka"):
        candidates = [target]
    else:
        import glob
        pattern = str(target).replace("\\", "/").rstrip("/") + "/**/*.mkv"
        candidates = [Path(p) for p in glob.glob(pattern, recursive=True)]

    todo, skipped = [], 0
    for mkv in candidates:
        if has_jp_subtitle(mkv):
            skipped += 1
        else:
            todo.append(mkv)
    return candidates, todo, skipped


def scan(target):
    """只报告不处理：列出哪些集会被处理、哪些已有日语轨。"""
    if not common.ensure_tools(("ffprobe",)):
        return 1
    target = Path(target)
    cands, todo, skipped = collect_mkvs(target)
    print(f"目标: {target}")
    print(f"  共 {len(cands)} 集 mkv：待处理 {len(todo)} 集，已有日语轨跳过 {skipped} 集")
    for mkv in todo:
        print(f"    待处理  {mkv.name}")
    if not todo:
        print("  没有需要处理的（都没有新集数，或者都已经有日语轨了）")
    return 0


def process(target, keep_srt=False, use_furigana=True, auto_download_tools=False):
    """处理入口：扫描目标 → 该做的做掉 → 打印汇总。返回退出码（有失败就是 1）。
    日志：控制台照旧显示，同时追加写一份到被扫描目录下的 anime_jp_sub.log。"""
    common.setup_console()
    target = Path(target)
    log = None
    if not os.environ.get("ANIME_JP_SUB_NO_LOG"):
        try:
            log = common.Tee(common.log_path(target))
            sys.stdout = log
            print(f"\n{'=' * 60}\n运行开始 {time.strftime('%Y-%m-%d %H:%M:%S')}  "
                  f"目标: {target}")
        except Exception as e:                      # noqa: BLE001
            print(f"  [warn] 打不开日志文件：{e}（只在控制台输出）")
    try:
        return _run(keep_srt, use_furigana, target, auto_download_tools)
    finally:
        if log is not None:
            sys.stdout = log.console
            log.close()


def _run(keep_srt, use_furigana, target, auto_download_tools=False):
    t_run = time.monotonic()

    # 连"要不要处理"都得靠 ffprobe 判断，所以先确认它在（缺了可以问一句自动下）
    if not common.ensure_tools(("ffprobe",), auto_download=auto_download_tools):
        return 1

    # 判定只看"有没有日语字幕轨"（内容判定，幂等）；已弃用"mtime vs 上次运行时刻"的时间闸
    candidates, to_process, skipped_done = collect_mkvs(target)
    print(f"[scan] total={len(candidates)}  new_to_process={len(to_process)}  "
          f"already_done(skip)={skipped_done}")

    # 要干活了才发现缺东西就太晚了——先自检（这一步不动任何文件）
    if to_process:
        problem = check_python_deps()
        if problem:
            print(f"\n[X] 不能开始处理：{problem}")
            return 1
        # 内封才用得到的两个，等确认有活干再查（没活干就不折腾用户下 88MB）
        if not common.ensure_tools(("ffmpeg", "mkvmerge", "mkvpropedit"),
                                   auto_download=auto_download_tools):
            return 1
        missing_model = check_model()
        if missing_model:
            print(f"\n[X] 不能开始处理：{missing_model}")
            return 1

    cand_pool = {}      # 番剧文件夹 -> 该文件夹这次处理过的所有句子(挑人名/专有名词用)
    done, failed = [], []      # 本次运行的结果（最后给汇总）
    for mkv in to_process:
        print(f"\n=== 处理: {mkv.name} ===")
        t_ep = time.monotonic()
        tmpdir = Path(mkv).parent / f".tmp_{mkv.stem}"
        tmpdir.mkdir(exist_ok=True)
        wav_path = tmpdir / "audio.wav"
        srt_path = tmpdir / "sub.srt"
        try:
            audios, _ = probe_streams(mkv)
            if not audios:
                print("  [!] 无音频轨，跳过")
                continue
            audio = pick_audio(mkv, audios)
            if audio is None:
                print("  [!] 无可选音频轨，跳过")
                continue

            # 提取中文字幕时间基准(用于断句切分)
            timeline, max_end, base_lang = extract_timeline_base(mkv)
            print(f"  timeline base: {len(timeline)} 字幕段 "
                  f"(lang={base_lang}), max_end={max_end:.1f}s")

            extract_wav(mkv, audio, wav_path)
            print("  audio track -> wav ok")

            # 视频尺寸/字号先拿好：日语侧断句要按"单行宽度"限制来拼行
            vw, vh = video_size(mkv)
            folder = Path(mkv).parent
            opt_main = cfg_int("furigana", "main_px", folder)
            align_mode = (common.setting("align", "mode", "jp", folder) or "jp").lower()
            seg_list, dur, qa = transcribe(wav_path, timeline, max_end,
                                           (vw, vh), opt_main, align_mode)
            print(f"  whisper done, duration={dur:.1f}s, 日语 {qa['n']} 条, "
                  f"覆盖 {qa['covered']:.0f}s/{qa['total']:.0f}s, "
                  f"语速>10字/s {qa['cram']} 条")

            # 字幕文件：默认生成带平假名注音的 ASS；--no-furigana 或注音模块不可用时退回 SRT
            if use_furigana and HAVE_FURIGANA:
                dict_path = find_dict(folder)
                # 配置分层：[furigana] 段可以覆盖默认字号/底边距/每行上限
                # （番剧文件夹里的 config.ini 优先于脚本同目录的全局 config.ini）
                opt_bottom = cfg_int("furigana", "bottom_px", folder)
                opt_chars = cfg_int("furigana", "max_chars", folder)
                ass, ms, rs = furigana.build_ass(
                    seg_list, vw, vh, str(dict_path) if dict_path else None,
                    opt_main, opt_chars, opt_bottom)
                sub_path = tmpdir / "sub.ass"
                sub_path.write_text(ass, encoding="utf-8")
                print(f"  注音 ASS: {furigana.FONT} 主字 {ms}px / 注音 {rs}px / "
                      f"PlayRes {vw}x{vh}" +
                      (f" / 词典 {dict_path.name}" if dict_path else ""))
                cand_pool.setdefault(str(Path(mkv).parent), []).extend(seg_list)
            else:
                if use_furigana and not HAVE_FURIGANA:
                    print(f"  [warn] 注音模块不可用({FURIGANA_ERR})，退回无注音 SRT")
                sub_path = write_srt(seg_list, srt_path)

            # 日语注音用随项目分发的字体：连字体内附进 mkv，观众没装字体也能正确渲染
            attach = str(furigana.font_file()) if (use_furigana and HAVE_FURIGANA) else None
            new_mkv, added_atts = mux_subtitle(mkv, sub_path, attach)
            print(f"  muxed -> {new_mkv}")

            # 自验证：不信 mkvmerge 自报，独立 ffprobe 复核，过了才替换原文件
            problems = verify_muxed(mkv, new_mkv, added_atts)
            if problems:
                # 自检没过的中间产物是坏成品，直接清掉，别在原目录留 .jp.mkv
                try:
                    Path(new_mkv).unlink()
                except Exception:
                    pass
                raise RuntimeError("内封后自检未通过: " + "; ".join(problems))

            replace_original(mkv, new_mkv)
            print("  replaced ok  [自检] jpn + Japanese 已确认, 字幕=日语+中文, 音视频 stream copy")
            done.append((mkv.name, time.monotonic() - t_ep))
        except Exception as e:
            print(f"  [X] 失败: {e}")
            # 汇总里只放一行摘要：ffprobe 的报错是多行 JSON，直接塞进汇总会把那一屏撑花
            failed.append((mkv.name, " ".join(str(e).split())[:200]))
        finally:
            # 清理临时 wav；字幕文件(srt/ass)除非 --keep-srt
            try:
                if wav_path.exists():
                    wav_path.unlink()
            except Exception:
                pass
            if not keep_srt:
                for leftover in (srt_path, tmpdir / "sub.ass"):
                    try:
                        if leftover.exists():
                            leftover.unlink()
                    except Exception:
                        pass
            try:
                tmpdir.rmdir()
            except Exception:
                pass

    # 收尾：按番剧文件夹维护人名词典——没有就按模板新建，末尾的"候选"段每次运行重写，
    # 用户自己写的条目不动。候选只列含汉字的固有名詞（片假名会自动转写，不用人管）。
    if use_furigana and HAVE_FURIGANA:
        for folder, segs in cand_pool.items():
            try:
                cands = furigana.collect_candidates(segs)
                p, changed, n = furigana.update_dict(folder, cands)
                print(f"[词典] {p}  候选 {n} 条" + ("（已更新）" if changed else "（无变化）"))
            except Exception as e:              # noqa: BLE001
                print(f"[词典] {folder} 更新失败: {e}")

    # ---- 本次运行汇总（批量跑完最需要看的一屏）----
    total_min = (time.monotonic() - t_run) / 60.0
    print(f"\n{'=' * 60}")
    print(" 本次运行汇总")
    print(f"   扫描到      : {len(candidates)} 集 mkv")
    print(f"   已有日语轨跳过: {skipped_done} 集")
    print(f"   新处理成功  : {len(done)} 集"
          + (f"（{sum(d for _, d in done) / 60.0:.1f} 分钟，平均 "
             f"{sum(d for _, d in done) / len(done) / 60.0:.1f} 分钟/集）" if done else ""))
    print(f"   失败        : {len(failed)} 集")
    for name, why in failed:
        print(f"     × {name}\n       {why}")
    print(f"   总用时      : {total_min:.1f} 分钟")
    print(f"{'=' * 60}")
    print("全部完成")
    return 1 if failed else 0


if __name__ == "__main__":
    # 直接跑这个模块（python -m anime_jp_sub.pipeline）时转给命令行入口
    from .cli import main as _cli_main
    sys.exit(_cli_main())

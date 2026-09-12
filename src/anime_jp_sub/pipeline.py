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
import json
import os
import re
import subprocess
import sys
import time
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

# 判定为"中文"字幕轨的语言标签(含简繁体常见变体)
CHINESE_LANGS = ("chi", "zh", "chs", "cht", "zh-cn", "zh-tw", "zh-hans", "zh-hant")


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
    CUDA 缺 DLL 那种错误是取第一条结果时才炸出来的，光 try 住 transcribe() 没用（踩过）。"""
    from faster_whisper import WhisperModel
    print(f"  [转写] 加载模型 large-v3（{device} / {compute_type}）...", flush=True)
    model = WhisperModel(model_path(), device=device, compute_type=compute_type)
    segments, info = model.transcribe(
        str(wav_path), language=LANG, beam_size=BEAM_SIZE, vad_filter=VAD_FILTER,
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


def transcribe(wav_path, timeline, max_end):
    """faster-whisper large-v3 听写，返回对齐好的 [(start, end, text), ...]。

    word_timestamps=True：只有拿到词级时间，才能把 Whisper"两句并一段"的输出
    重新切回两条中文台词的边界上。
    condition_on_previous_text=False：音乐/OP 段容易触发"重复上一句"的幻觉滚雪球
    (实测关掉 VAD 时会连出 5 条「ご視聴ありがとうございました」)，关掉这个开关
    可以让幻觉不再往下传。
    ⚠ CUDA 起不来（缺 cublas/cudnn DLL）时**自动改用 CPU 重跑**：以前只在加载前
    `get_cuda_device_count()` 判断，装了显卡驱动但没装 CUDA 运行库的机器会通过这个检查、
    然后在推理时炸掉（clean 环境实测）。
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

    # 按中文断句边界切分词级时间轴
    seg_list = align_timeline(seg_list, timeline, max_end, total_dur)
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

            seg_list, dur, qa = transcribe(wav_path, timeline, max_end)
            print(f"  whisper done, duration={dur:.1f}s, 日语 {qa['n']} 条, "
                  f"覆盖 {qa['covered']:.0f}s/{qa['total']:.0f}s, "
                  f"语速>10字/s {qa['cram']} 条")

            # 字幕文件：默认生成带平假名注音的 ASS；--no-furigana 或注音模块不可用时退回 SRT
            if use_furigana and HAVE_FURIGANA:
                vw, vh = video_size(mkv)
                folder = Path(mkv).parent
                dict_path = find_dict(folder)
                # 配置分层：[furigana] 段可以覆盖默认字号/底边距/每行上限
                # （番剧文件夹里的 config.ini 优先于脚本同目录的全局 config.ini）
                opt_main = cfg_int("furigana", "main_px", folder)
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

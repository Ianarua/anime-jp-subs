# -*- coding: utf-8 -*-
"""align_timeline 的测试 —— 时间轴对齐是整个项目最容易出 bug 的地方。

历史教训（都写成了用例）：
  * 「整段吸附」会把 Whisper 并起来的两句塞成一条，另一条彻底没字幕；
  * 一句被拆成两段时，同一中文区间被均分，语速飙到 10~18 字/秒。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from anime_jp_sub import common  # noqa: E402
from anime_jp_sub import pipeline as ajs  # noqa: E402


class TestAlignTimeline(unittest.TestCase):
    CN = [(10.0, 12.0), (12.5, 15.0)]

    def test_merged_sentence_is_split_across_two_cn_lines(self):
        """一段话横跨两条中文台词 → 必须切成两条，各用各自的中文区间。"""
        segs = [(9.8, 15.2, "x", [(9.9, 10.6, "あ"), (10.8, 11.7, "い"),
                                  (12.6, 13.5, "う")])]
        self.assertEqual(ajs.align_timeline(segs, self.CN, 15.0, 20.0),
                         [(10.0, 12.0, "あい"), (12.5, 15.0, "う")])

    def test_orphan_words_keep_whisper_time(self):
        """离中文区间超过容差的词：保留 Whisper 自己的时间，不能丢。"""
        segs = [(20.0, 22.0, "x", [(20.0, 20.7, "と"), (21.0, 21.8, "お")])]
        self.assertEqual(ajs.align_timeline(segs, self.CN, 15.0, 30.0),
                         [(20.0, 21.8, "とお")])

    def test_head_fragment_moves_to_next_line(self):
        """中文边界切在词内部（ス|ポーツ）→ 前半截要挪回下一句。

        用户实测：片假名/汉字词被 BPE 切成两段后，前半截挂在上一句末尾
        （一集几十处：実|は、学|院、あ|なた、五|木…）。
        """
        segs = [(29.0, 31.0, "x", [(30.0, 30.5, "ス"), (30.5, 31.0, "ポーツ")])]
        cn = [(28.0, 30.4), (30.4, 31.5)]
        self.assertEqual(ajs.align_timeline(segs, cn, 31.5, 31.5),
                         [(30.4, 31.5, "スポーツ")])   # 上一句空了 → 不留空条目

    def test_sentence_final_particle_is_not_moved(self):
        """上一句句尾是助動詞/助詞（だ・な）时不能挪：だ|が 拼起来是 だが，
        但那多半是两句的分界（上一句的句尾），搬走会把上一句弄残。"""
        segs = [(0.0, 1.0, "x", [(0.2, 0.5, "だ"), (0.5, 0.9, "が")])]
        cn = [(0.0, 0.55), (0.55, 1.2)]
        self.assertEqual(ajs.align_timeline(segs, cn, 1.2, 1.2),
                         [(0.0, 0.55, "だ"), (0.55, 1.2, "が")])

    def test_pause_blocks_the_repair(self):
        """两个碎片中间有明显停顿 → 本来就是两个词，别硬拼起来。"""
        segs = [(0.0, 2.0, "x", [(0.2, 0.5, "実"), (0.9, 1.3, "は")])]
        cn = [(0.0, 0.55), (0.55, 1.5)]
        self.assertEqual(ajs.align_timeline(segs, cn, 1.5, 1.5),
                         [(0.0, 0.55, "実"), (0.55, 1.5, "は")])

    def test_no_timeline_falls_back_to_word_time(self):
        """片源没有中文字幕轨时，按词级时间成条（不能整段吸附到空气上）。"""
        segs = [(20.0, 22.0, "x", [(20.0, 20.7, "と"), (21.0, 21.8, "お")])]
        self.assertEqual(ajs.align_timeline(segs, [], 0.0, 30.0),
                         [(20.0, 21.8, "とお")])

    def test_segment_without_words_uses_segment_time(self):
        segs = [(10.2, 11.8, "まるごと", [])]
        self.assertEqual(ajs.align_timeline(segs, self.CN, 15.0, 20.0),
                         [(10.0, 12.0, "まるごと")])

    def test_cap_protection(self):
        """超过音频时长的条目要被夹住（防 60 倍时间戳那种爆表）。"""
        segs = [(99.0, 100.0, "ばか", [(99.0, 100.0, "ばか")])]
        out = ajs.align_timeline(segs, self.CN, 15.0, 20.0)
        self.assertLessEqual(out[-1][1], 20.0)

    def test_non_adjacent_groups_in_same_interval_are_merged(self):
        """同一个中文区间被切成不相邻的两组：合并成一条，避免同时间重复显示。"""
        segs = [(10.0, 15.0, "x", [(10.1, 10.5, "あ"), (12.8, 13.2, "い"),
                                   (13.4, 13.9, "う")])]
        self.assertEqual(ajs.align_timeline(segs, self.CN, 15.0, 20.0),
                         [(10.0, 12.0, "あ"), (12.5, 15.0, "いう")])


class TestMediaSignature(unittest.TestCase):
    """回归：自检不能比对附件的 codec_name。
    用户实测：原始下载文件里附件报 ttf/otf，mkvmerge 重新封装后同样的附件报 None，
    旧代码据此判定"视频/音频轨发生变化"，把整集判失败。"""

    OLD = [{"codec_type": "video", "codec_name": "h264"},
           {"codec_type": "audio", "codec_name": "aac"},
           {"codec_type": "subtitle", "codec_name": "ass"},
           {"codec_type": "attachment", "codec_name": "otf",
            "tags": {"filename": "AdobeArabic-Bold.otf"}},
           {"codec_type": "attachment", "codec_name": "ttf",
            "tags": {"filename": "arial.ttf"}}]
    NEW = [{"codec_type": "video", "codec_name": "h264"},
           {"codec_type": "audio", "codec_name": "aac"},
           {"codec_type": "subtitle", "codec_name": "ass"},
           {"codec_type": "attachment", "codec_name": None,
            "tags": {"filename": "AdobeArabic-Bold.otf"}},
           {"codec_type": "attachment", "codec_name": None,
            "tags": {"filename": "arial.ttf"}}]

    def test_attachments_codec_name_change_is_not_a_problem(self):
        self.assertEqual(ajs.media_signature(self.OLD), ajs.media_signature(self.NEW))

    def test_video_audio_codec_change_is_detected(self):
        broken = [dict(s) for s in self.NEW]
        broken[0]["codec_name"] = "hevc"          # 被重编码了
        self.assertNotEqual(ajs.media_signature(self.OLD), ajs.media_signature(broken))

    def test_attachment_loss_changes_signature(self):
        missing = [s for s in self.NEW
                   if s.get("tags", {}).get("filename") != "arial.ttf"]
        self.assertNotEqual(ajs.media_signature(self.OLD), ajs.media_signature(missing))


class TestModelCheck(unittest.TestCase):
    """模型这块的规矩（用户定的）：**不自动下载**，只提示用户自己去下、手动放进 models\\。

    踩过的坑：原来写的是自动下 openai/whisper-large-v3 —— 那个仓库是 Transformers 格式，
    没有 model.bin，hf_hub_download 直接 404，新人根本下不下来（清洁环境实测触发）。"""

    def test_no_auto_download_anymore(self):
        """回归：不许再冒出自动下模型的代码（用户明确要求手动放）。"""
        for name in ("download_model", "model_is_ready"):
            self.assertFalse(hasattr(ajs, name), f"{name} 又冒出来了")

    def test_cli_has_no_download_model_flag(self):
        """回归：命令行也不该再有 --download-model。"""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        from anime_jp_sub import cli
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["process", "--download-model"])

    def test_repo_is_the_ctranslate2_one(self):
        """提示里给的仓库必须是 CTranslate2 格式那个，否则用户下回来 faster-whisper 读不了。"""
        self.assertEqual(ajs.MODEL_REPO, "Systran/faster-whisper-large-v3")
        self.assertNotEqual(ajs.MODEL_REPO, "openai/whisper-large-v3")

    def test_missing_model_message_mentions_how_to_get_it(self):
        import os
        import tempfile
        saved = os.environ.get("ANIME_JP_SUB_MODEL_DIR")
        try:
            with tempfile.TemporaryDirectory() as d:
                os.environ["ANIME_JP_SUB_MODEL_DIR"] = d
                common._CONFIG_CACHE.clear()
                msg = ajs.check_model()
            self.assertIsNotNone(msg)
            self.assertIn(ajs.MODEL_REPO, msg)         # 告诉去哪个仓库下
            self.assertIn("hf-mirror.com", msg)        # 国内镜像也给出来
            self.assertIn("model.bin", msg)            # 告诉要哪些文件
            self.assertIn("vocabulary.json", msg)
            self.assertIn("aria2c", msg)               # 3GB 要能续传
            self.assertIn("model_dir", msg)            # 告诉可以改配置换位置
            self.assertIn(ajs.MODEL_REPO_BAD, msg)     # 点名"别下那个仓库"
            self.assertNotIn("自动下载吗", msg)         # 不再问要不要自动下
        finally:
            if saved is None:
                os.environ.pop("ANIME_JP_SUB_MODEL_DIR", None)
            else:
                os.environ["ANIME_JP_SUB_MODEL_DIR"] = saved
            common._CONFIG_CACHE.clear()


class TestCudaFallback(unittest.TestCase):
    """CUDA 起不来时要自动改用 CPU 重跑。

    真实场景（清洁环境实测抓到）：机器装了 N 卡驱动但没装 CUDA 运行库时，
    `ctranslate2.get_cuda_device_count()` 照样返回 1，于是选 CUDA，然后在推理时
    炸「Library cublas64_12.dll is not found or cannot be loaded」——整集直接失败。
    """

    def test_recognizes_cuda_runtime_errors(self):
        self.assertTrue(ajs._cuda_runtime_error(RuntimeError(
            "Library cublas64_12.dll is not found or cannot be loaded")))
        self.assertTrue(ajs._cuda_runtime_error(
            RuntimeError("Library cudnn_ops_infer64_8.dll is not found")))
        self.assertFalse(ajs._cuda_runtime_error(RuntimeError("找不到模型目录")))
        self.assertFalse(ajs._cuda_runtime_error(ValueError("bad srt")))

    def _fake_module(self, used, boom_on_cuda=True, other_error=False):
        import types

        class FakeModel:
            def __init__(self, path, device, compute_type):
                used.append(device)
                if other_error and device == "cuda":
                    raise RuntimeError("模型文件坏了")
                if boom_on_cuda and device == "cuda":
                    raise RuntimeError(
                        "Library cublas64_12.dll is not found or cannot be loaded")

            def transcribe(self, *a, **kw):
                return iter([]), types.SimpleNamespace(duration=10.0)

        mod = types.ModuleType("faster_whisper")
        mod.WhisperModel = FakeModel
        return mod

    def test_falls_back_to_cpu_when_cuda_fails(self):
        from unittest import mock
        used = []
        with mock.patch.dict(sys.modules, {"faster_whisper": self._fake_module(used)}), \
             mock.patch.object(ajs, "pick_device", lambda: ("cuda", "int8")):
            segs, dur, _qa = ajs.transcribe("x.wav", [], 10.0)
        self.assertEqual(used, ["cuda", "cpu"])       # 先试 CUDA，失败后自动 CPU
        self.assertEqual(dur, 10.0)
        self.assertEqual(segs, [])

    def test_unrelated_errors_are_not_swallowed(self):
        from unittest import mock
        used = []
        with mock.patch.dict(sys.modules,
                             {"faster_whisper": self._fake_module(used, other_error=True)}), \
             mock.patch.object(ajs, "pick_device", lambda: ("cuda", "int8")):
            with self.assertRaises(RuntimeError) as cm:
                ajs.transcribe("x.wav", [], 10.0)
        self.assertIn("模型文件坏了", str(cm.exception))
        self.assertEqual(used, ["cuda"])              # 不该悄悄吞掉别的错


class TestQaStats(unittest.TestCase):
    def test_counts_crammed_entries(self):
        entries = [(0.0, 4.0, "あ" * 20),        # 5 字/秒，正常
                   (5.0, 6.0, "あ" * 15)]        # 15 字/秒，太挤
        qa = ajs.qa_stats(entries, 100.0)
        self.assertEqual(qa["n"], 2)
        self.assertEqual(qa["cram"], 1)
        self.assertAlmostEqual(qa["covered"], 5.0, places=6)


if __name__ == "__main__":
    unittest.main()

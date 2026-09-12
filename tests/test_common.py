# -*- coding: utf-8 -*-
"""common.py 的测试：时间戳换算、SRT 解析。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from anime_jp_sub import common  # noqa: E402


class TestConfigLayers(unittest.TestCase):
    """配置分层：全局 config.ini → 番剧文件夹里的 config.ini（后者覆盖前者）。"""

    def test_layer_precedence(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            show = d / "某番"
            show.mkdir()
            (d / "config.ini").write_text(
                "[furigana]\nmain_px = 60\n\n[paths]\nffmpeg = C:\\global\\ffmpeg.exe\n",
                encoding="utf-8")
            (show / "config.ini").write_text(
                "[furigana]\nmain_px = 72\n", encoding="utf-8")
            old = os.environ.get("ANIME_JP_SUB_CONFIG")
            os.environ["ANIME_JP_SUB_CONFIG"] = str(d / "config.ini")
            common._CONFIG_CACHE.clear()
            try:
                # 番剧级覆盖全局
                self.assertEqual(common.setting("furigana", "main_px", folder=show), "72")
                # 全局里的其它键仍然可见
                self.assertEqual(common.setting("paths", "ffmpeg"), "C:\\global\\ffmpeg.exe")
                # 没配的项走代码默认值
                self.assertEqual(common.setting("furigana", "bottom_px", "50", folder=show), "50")
            finally:
                if old is None:
                    os.environ.pop("ANIME_JP_SUB_CONFIG", None)
                else:
                    os.environ["ANIME_JP_SUB_CONFIG"] = old
                common._CONFIG_CACHE.clear()


class TestLogFile(unittest.TestCase):
    def test_missing_tool_gives_actionable_error(self):
        """回归：新人没装 ffmpeg 时，要给出"装它 / 写 config.ini / 设环境变量"这种能照着做的
        提示，而不是一句 FileNotFoundError。做法是把 PATH 清空、把项目 tools/ 也指向不存在的地方。"""
        import os
        saved_path = os.environ.get("PATH", "")
        saved_env = os.environ.pop("ANIME_JP_SUB_FFMPEG", None)
        saved_cfg = common._CONFIG_CACHE.get("")
        saved_tools_dir = common.tools_dir
        try:
            os.environ["PATH"] = ""
            os.environ.pop("ANIME_JP_SUB_FFMPEG", None)
            common.tools_dir = lambda: Path(r"X:\definitely\not\here\tools")
            common._CONFIG_CACHE[""] = {}
            common._TOOL_CACHE.clear()      # 别让别的用例缓存过的路径混进来
            with self.assertRaises(RuntimeError) as cm:
                common.find_tool("ffmpeg")
            msg = str(cm.exception)
            self.assertIn("找不到 ffmpeg", msg)
            self.assertIn("ANIME_JP_SUB_FFMPEG", msg)     # 告诉用户可以设哪个环境变量
            self.assertIn(common.CONFIG_NAME, msg)        # 也说可以写配置文件
        finally:
            os.environ["PATH"] = saved_path
            common._TOOL_CACHE.clear()
            common.tools_dir = saved_tools_dir
            common._CONFIG_CACHE.clear()
            if saved_cfg is not None:
                common._CONFIG_CACHE[""] = saved_cfg
            if saved_env is not None:
                os.environ["ANIME_JP_SUB_FFMPEG"] = saved_env

    def test_log_path_defaults_to_scanned_dir(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "a.mkv").write_bytes(b"")
            self.assertEqual(common.log_path(p), p / "anime_jp_sub.log")      # 传目录
            self.assertEqual(common.log_path(p / "a.mkv"), p / "anime_jp_sub.log")  # 传单集

    def test_tee_writes_to_console_and_file(self):
        import io
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            t = common.Tee(Path(d) / "x.log", console=buf)
            t.write("hello 日志\n")
            t.flush()
            t.close()
            self.assertEqual(buf.getvalue(), "hello 日志\n")
            self.assertEqual((Path(d) / "x.log").read_text(encoding="utf-8"), "hello 日志\n")


class TestTimestamps(unittest.TestCase):
    def test_fmt_srt_ts(self):
        self.assertEqual(common.fmt_srt_ts(0), "00:00:00,000")
        self.assertEqual(common.fmt_srt_ts(1.5), "00:00:01,500")
        self.assertEqual(common.fmt_srt_ts(3661.25), "01:01:01,250")

    def test_fmt_ass_ts(self):
        self.assertEqual(common.fmt_ass_ts(0), "0:00:00.00")
        self.assertEqual(common.fmt_ass_ts(1.5), "0:00:01.50")
        self.assertEqual(common.fmt_ass_ts(3661.25), "1:01:01.25")

    def test_srt_and_ass_agree_on_seconds(self):
        """同一个秒数，两种格式表示的是同一时刻（防止哪天改坏一个忘了另一个）。"""
        for sec in (0.0, 12.34, 61.0, 3599.99):
            s = common.fmt_srt_ts(sec).replace(",", ".")
            a = common.fmt_ass_ts(sec)
            self.assertAlmostEqual(float(s.split(":")[0]) * 3600
                                   + float(s.split(":")[1]) * 60
                                   + float(s.split(":")[2]),
                                   float(a.split(":")[0]) * 3600
                                   + float(a.split(":")[1]) * 60
                                   + float(a.split(":")[2]), places=2)


class TestSrtParsing(unittest.TestCase):
    SRT = ("1\n"
           "00:00:01,000 --> 00:00:02,500\n"
           "一行目\n"
           "二行目\n"
           "\n"
           "2\n"
           "00:01:00.250 --> 00:01:02.000\n"
           "だけ\n")

    def test_parse_srt_timeline_accepts_both_separators(self):
        # 兼容 HH:MM:SS,mmm 和 HH:MM:SS.mmm
        self.assertEqual(common.parse_srt_timeline(self.SRT),
                         [(1.0, 2.5), (60.25, 62.0)])

    def test_load_srt_joins_multiline_and_skips_index(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.srt"
            p.write_text(self.SRT, encoding="utf-8")
            entries = common.load_srt(p)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0][0], 1.0)
        self.assertEqual(entries[0][1], 2.5)
        self.assertEqual(entries[0][2], "一行目 二行目")   # 多行合并成一条
        self.assertEqual(entries[1][2], "だけ")


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""内封链路的集成测试：真的调 ffmpeg / mkvmerge，造一个 2 秒的小片子跑完整流程。

需要外部程序（ffmpeg + MKVToolNix），没装就自动跳过——所以在 CI 上也能跑。
这里覆盖的是"合成素材"，**不涉及任何番剧片段**（版权）。
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from anime_jp_sub import common  # noqa: E402
from anime_jp_sub import furigana as fg  # noqa: E402
from anime_jp_sub import pipeline as ajs  # noqa: E402


def _tools_ready():
    try:
        return all(Path(common.find_tool(k)).is_file()
                   for k in ("ffmpeg", "ffprobe", "mkvmerge", "mkvpropedit"))
    except Exception:                                  # noqa: BLE001
        return False


@unittest.skipUnless(_tools_ready(), "需要 ffmpeg + MKVToolNix 才能跑集成测试")
class TestMuxChain(unittest.TestCase):
    def test_mux_then_verify_with_new_font_attachment(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base = d / "base.mkv"
            # 2 秒的合成测试片（没有音频内容、没有字幕——只验证链路）
            code, out = common.run([
                common.FFMPEG, "-y", "-v", "error",
                "-f", "lavfi", "-i", "color=c=black:s=320x240:d=2",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                "-shortest", str(base)], timeout=120)
            self.assertEqual(code, 0, f"ffmpeg 生成测试片失败: {out[-500:]}")

            # 用它生成一份注音 ASS
            ass, _, _ = fg.build_ass([(0.2, 1.0, "暗き神々の像か")], 1920, 1080)
            sub = d / "sub.ass"
            sub.write_text(ass, encoding="utf-8")

            # 内封 + 内附字体，然后自检
            out_mkv, added = ajs.mux_subtitle(str(base), str(sub),
                                              attach_font=str(fg.font_file()))
            self.assertEqual(added, 1, "应该内附了 1 个字体")
            self.assertEqual(ajs.verify_muxed(str(base), out_mkv, added), [],
                             "自检必须通过（附件 codec_name 变化不算问题）")

            # 独立复核：日语轨在最前、有日语标签、字体在里面
            streams = ajs.probe_json(out_mkv)["streams"]
            subs = [s for s in streams if s.get("codec_type") == "subtitle"]
            self.assertEqual(len(subs), 1)
            self.assertEqual(subs[0]["tags"].get("language"), "jpn")
            self.assertEqual(subs[0]["tags"].get("title"), "Japanese")
            names = [(s.get("tags", {}).get("filename") or "")
                     for s in streams if s.get("codec_type") == "attachment"]
            self.assertIn(Path(fg.font_file()).name, names)

    def test_second_run_does_not_duplicate_font(self):
        """重跑时字体已在文件里 → 不重复内附（否则每次重跑都多一个附件）。"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base = d / "base.mkv"
            common.run([common.FFMPEG, "-y", "-v", "error",
                        "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1",
                        "-c:v", "libx264", "-preset", "ultrafast", str(base)],
                       timeout=120)
            ass, _, _ = fg.build_ass([(0.2, 0.9, "神々の像")], 1920, 1080)
            sub = d / "sub.ass"
            sub.write_text(ass, encoding="utf-8")

            first, added1 = ajs.mux_subtitle(str(base), str(sub),
                                             attach_font=str(fg.font_file()))
            self.assertEqual(added1, 1)
            # 用第一次的产物当输入再跑一次（模拟"这集重新生成字幕"）
            again, added2 = ajs.mux_subtitle(first, str(sub),
                                             attach_font=str(fg.font_file()))
            self.assertEqual(added2, 0, "字体已存在时不应重复内附")
            names = [(s.get("tags", {}).get("filename") or "")
                     for s in ajs.probe_json(again)["streams"]
                     if s.get("codec_type") == "attachment"]
            self.assertEqual(names.count(Path(fg.font_file()).name), 1)


if __name__ == "__main__":
    unittest.main()

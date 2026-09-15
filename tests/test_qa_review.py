# -*- coding: utf-8 -*-
"""评审页（qa/review）：可疑点标注 / 行数据 / 页面与文本产物。

这一块是"人工评审 → 调参"流程的入口，宁可多测几条：
  * 可疑点的四类标注（跨停顿 / 行首助詞 / 行尾起句 / 偏长）
  * ASS 只取正文（注音是另一行，样式 RB，绝不能混进来）
  * 生成的 HTML 里数据是内嵌的（页面不联网、双击就开）
"""

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from anime_jp_sub.qa import review  # noqa: E402


class TestFlags(unittest.TestCase):
    def test_pause_covered_by_the_line_is_flagged(self):
        """整条字幕把一段长静音盖住 → 很可能是两句并成一句。"""
        f = review.flags_for("はいクリーム", 25.0, 32.0, [(26.7, 30.5)])
        self.assertIn("⚠跨停顿 3.8s", " ".join(f))

    def test_pause_outside_the_line_is_not_flagged(self):
        # 停顿在**这条之外**（前一条的尾巴）→ 不该标这条
        f = review.flags_for("はいクリーム", 25.0, 26.0, [(26.7, 30.5)])
        self.assertFalse([x for x in f if "跨停顿" in x])

    def test_line_starting_with_particle_is_flagged(self):
        self.assertTrue([x for x in review.flags_for("に頼まれて", 1.0, 3.0, [])
                         if "行首助詞" in x])

    def test_interjection_ending_is_flagged(self):
        self.assertTrue([x for x in review.flags_for("かなっていや", 1.0, 2.0, [])
                         if "行尾起句" in x])
        # 整行都是起句词（「ああはい」）→ 是独立应答，不该标
        self.assertFalse([x for x in review.flags_for("ああはい", 1.0, 2.0, [])
                          if "行尾起句" in x])

    def test_long_line_is_flagged(self):
        self.assertTrue([x for x in review.flags_for("あ" * 30, 1.0, 3.0, [])
                         if "偏长" in x])
        self.assertTrue([x for x in review.flags_for("あ", 1.0, 10.0, [])
                         if "偏长" in x])
        self.assertFalse([x for x in review.flags_for("あいうえお", 1.0, 3.0, [])
                          if "偏长" in x])


class TestRowsAndTxt(unittest.TestCase):
    def test_rows_number_and_round(self):
        rows = review.build_rows([(0.123456, 1.98765, "こんにちは")], [])
        self.assertEqual(rows[0]["n"], 1)
        self.assertEqual(rows[0]["s"], 0.12)
        self.assertEqual(rows[0]["e"], 1.99)
        self.assertEqual(rows[0]["t"], "こんにちは")

    def test_txt_has_one_line_per_entry(self):
        rows = review.build_rows([(0.0, 1.0, "はい"), (1.5, 3.0, "そうです")], [])
        txt = review.render_txt(rows, "测试集 E01")
        self.assertIn("测试集 E01", txt)
        self.assertIn("はい", txt)
        self.assertEqual(sum(1 for ln in txt.splitlines() if ln.startswith("00")), 2)


class TestAssParsing(unittest.TestCase):
    def test_only_main_style_is_read(self):
        """注音是独立的 Dialogue 行（样式 RB）——只能取正文，否则一条字幕变三条。"""
        import tempfile
        ass = ("[Events]\n"
               "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
               "Dialogue: 0,0:00:07.85,0:00:11.26,JP,,0,0,0,,{\\an7\\pos(375,970)}6番さん\n"
               "Dialogue: 0,0:00:07.85,0:00:11.26,RB,,0,0,0,,{\\an7\\pos(408,937)}ばん\n")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.ass"
            p.write_text(ass, encoding="utf-8")
            entries = review.read_ass_entries(p)
        self.assertEqual(entries, [(7.85, 11.26, "6番さん")])


class TestHtml(unittest.TestCase):
    def test_data_is_embedded_and_placeholders_gone(self):
        rows = review.build_rows([(0.0, 1.0, "はい"), (2.0, 3.5, "テスト")], [])
        html = review.render_html(rows, "E01")
        self.assertNotIn("__DATA__", html)
        self.assertNotIn("__META__", html)
        m = re.search(r"const DATA = (\[.*?\]);", html, re.S)
        self.assertIsNotNone(m)
        data = json.loads(m.group(1))
        self.assertEqual(len(data), 2)
        self.assertEqual(data[1]["t"], "テスト")
        self.assertIn("E01", html)

    def test_template_is_shipped_with_the_package(self):
        self.assertTrue(review.template_path().is_file())
        self.assertIn("__DATA__", review.template_path().read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

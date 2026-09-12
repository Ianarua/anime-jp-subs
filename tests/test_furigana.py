# -*- coding: utf-8 -*-
"""furigana.py 的测试：假名、送假名对齐、字体度量、词典、拆句、ASS 生成。

这里放的**全部是纯逻辑**（不跑 whisper、不碰视频），所以跑得快、也不需要模型。
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from anime_jp_sub import furigana as fg  # noqa: E402


class TestKana(unittest.TestCase):
    def test_katakana_to_hiragana(self):
        self.assertEqual(fg.to_hira("ハイロード"), "はいろーど")

    def test_halfwidth_katakana_normalized(self):
        # 半角片假名走 NFKC（当初用固定偏移算错过）
        self.assertEqual(fg.to_hira("ﾊｲﾛｰﾄﾞ"), "はいろーど")

    def test_voiced_and_small_kana(self):
        self.assertEqual(fg.to_hira("ヴァイオリン"), "ゔぁいおりん")

    def test_hiragana_and_kanji_untouched(self):
        self.assertEqual(fg.to_hira("あ漢"), "あ漢")


class TestRubyParts(unittest.TestCase):
    """送假名对齐：词尾、词中夹假名、没有假名锚点 三种情况。"""

    def test_trailing_okurigana(self):
        self.assertEqual(fg.ruby_parts("食べる", "たべる"), [(0, 1, "た")])

    def test_interior_kana_not_annotated(self):
        # 連れ去り -> 連(つ) れ 去(さ) り：中间的 れ 不该被注音盖住
        self.assertEqual(fg.ruby_parts("連れ去り", "つれさり"),
                         [(0, 1, "つ"), (2, 1, "さ")])

    def test_no_kana_anchor_annotates_whole_word(self):
        self.assertEqual(fg.ruby_parts("今日", "きょう"), [(0, 2, "きょう")])

    def test_rendaku_for_iteration_mark(self):
        # 神々 被 Janome 拆成 神 + 々 时，合并后要连浊（かみ + がみ）。
        # 注意：这一步返回的读音是"片假名 + 平假名"的中间态（连浊部分按平假名生成），
        # 真正的注音还要再经 to_hira 统一成平假名——真实链路就是这么走的。
        merged = fg.merge_iteration_marks([("神", "カミ"), ("々", "々")])
        self.assertEqual([s for s, _ in merged], ["神々"])
        self.assertEqual(fg.to_hira(merged[0][1]), "かみがみ")


class TestFontMetrics(unittest.TestCase):
    """字体度量：这些是踩过坑之后立下的规矩，改动字体时必须继续成立。"""

    def setUp(self):
        self.m = fg.metrics()

    def test_cjk_is_one_em(self):
        self.assertAlmostEqual(self.m.em("あ"), 1.0, places=2)
        self.assertAlmostEqual(self.m.em("漢"), 1.0, places=2)

    def test_punctuation_width_is_measured_not_assumed(self):
        # 回归：M PLUS 1 Code 的 '！' 是 0.5em，而 MS Gothic 是 1em。
        # 代码里**不能**再写"非 ASCII 就是 1em"，必须问字体文件。
        # 注意：要测**排版真正用的那个函数**（char_em/text_em），
        # 只测 metrics().em() 会被"有人把 char_em 改回硬编码假设"骗过去。
        self.assertLess(fg.char_em("！"), 0.8)
        self.assertGreater(fg.char_em("、"), 0.8)
        self.assertAlmostEqual(fg.text_em("あ！"),
                               fg.char_em("あ") + fg.char_em("！"), places=6)

    def test_line_width_uses_measured_widths(self):
        # 整行宽度也必须走实测：含半角标点的行要真的更窄
        with_punct = fg.text_em("本当に！")
        without = fg.text_em("本当にあ")
        self.assertLess(with_punct, without)

    def test_line_height_compensation_is_applied(self):
        # 回归：libass 按 unitsPerEm/(winAsc+winDesc) 缩放字号。
        # 该字体行高比 ≈1.5，所以 ASS 里写的字号必须**大于**目标像素。
        self.assertGreater(self.m.ass_size(60), 60)
        self.assertAlmostEqual(60 / self.m.scale, self.m.ass_size(60), places=3)

    def test_scale_matches_font_header(self):
        upem, asc, desc = fg.read_font_metrics(fg.font_file())
        self.assertAlmostEqual(self.m.scale, upem / (asc + desc), places=6)


class TestDictionary(unittest.TestCase):
    def test_parse_various_separators(self):
        text = "# 注释\n\n小玉\tたま\n菲莉, ふぃり\n蕾菲 れふぃ\n"
        self.assertEqual(fg.parse_dict_text(text),
                         {"小玉": "たま", "菲莉": "ふぃり", "蕾菲": "れふぃ"})

    def test_three_field_line_keeps_only_first_two(self):
        # 候选行去掉行首 # 之后带着"出现 N 次"，不能被当成读音的一部分
        self.assertEqual(fg.parse_dict_text("小玉\tこだま\t出现 12 次"), {"小玉": "こだま"})

    def test_longest_match_wins(self):
        d = fg.Dictionary({"小": "しょう", "小玉": "たま"})
        self.assertEqual(d.match("小玉が来た", 0), ("小玉", "たま"))

    def test_spans_use_dictionary_before_tokenizer(self):
        # 回归：Janome 把「小玉」拆成两个普通名词，靠词典整串命中才注得对
        an = fg.Analyzer()
        spans = fg.spans_for_text("小玉", an, fg.Dictionary({"小玉": "たま"}))
        self.assertEqual(spans, [(0, 2, "たま")])


class TestDictFile(unittest.TestCase):
    def test_create_preserve_and_refresh(self):
        with tempfile.TemporaryDirectory() as d:
            p, changed, n = fg.update_dict(d, [("小玉", "こだま", 12), ("王都", "おうと", 5)])
            self.assertTrue(changed)
            self.assertEqual(n, 2)
            text = Path(p).read_text(encoding="utf-8")
            self.assertIn(fg.DICT_MARK, text)
            self.assertIn("小玉", text)

            # 用户手写一条，再跑一次：用户条目必须留住，且该词从候选里消失
            Path(p).write_text(text.replace(fg.DICT_USER_MARK + "\n",
                                            fg.DICT_USER_MARK + "\n小玉\tたま\n"),
                               encoding="utf-8")
            p2, changed2, n2 = fg.update_dict(d, [("小玉", "こだま", 12), ("王都", "おうと", 5)])
            text2 = Path(p2).read_text(encoding="utf-8")
            self.assertEqual(fg.Dictionary.load(p2).entries, {"小玉": "たま"})   # 用户条目生效
            self.assertEqual(n2, 1)                                             # 小玉 已从候选剔除
            self.assertIn("#王都", text2)                                       # 候选段照常刷新


class TestSplitting(unittest.TestCase):
    def test_short_line_untouched(self):
        self.assertEqual(fg.split_long("短い", 10), ["短い"])

    def test_long_line_split_at_punctuation(self):
        text = "これは長い文章です。そして続きがあります。さらに続きます。"
        parts = fg.split_long(text, 15)
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(parts), text)               # 拆完拼回来必须一模一样
        self.assertTrue(parts[0].endswith("。"), f"第一刀应落在句号后: {parts[0]!r}")
        for p in parts[:-1]:
            self.assertLessEqual(len(p), 15)

    def test_expand_long_keeps_total_duration(self):
        out = fg.expand_long([(10.0, 20.0, "あ" * 45)], limit=20)
        self.assertGreater(len(out), 1)
        self.assertAlmostEqual(out[0][0], 10.0, places=6)
        self.assertAlmostEqual(sum(e - s for s, e, _ in out), 10.0, places=6)
        self.assertEqual("".join(t for _, _, t in out), "あ" * 45)
        for _, _, t in out[:-1]:
            self.assertLessEqual(len(t), 20)


class TestBuildAss(unittest.TestCase):
    ENTRIES = [(0.0, 2.0, "暗き神々の像か"),
               (3.0, 6.0, "ミルカを危ない目にあわせるわけにはいかないわ")]

    def test_build_and_layout(self):
        ass, ms, rs = fg.build_ass(self.ENTRIES, 1920, 1080)
        self.assertEqual(ms, fg.limits(1920, 1080)[0])          # 没被长句压小
        self.assertEqual(rs, fg.limits(1920, 1080)[1])
        self.assertIn("PlayResX: 1920", ass)
        self.assertIn(f"Style: JP,{fg.FONT}", ass)
        # ASS 里的字号是"补偿后"的（该字体行高比 1.5，写 60 会渲染成 40）
        self.assertIn(f"{ms / fg.metrics().scale:.1f}", ass)
        self.assertIn("暗き神々の像か", ass)
        self.assertIn("くら", ass)                              # 暗(くら)
        self.assertIn("かみがみ", ass)                          # 神々(かみがみ) —— 叠字连浊

    def test_ruby_stays_inside_frame(self):
        ass, ms, rs = fg.build_ass(self.ENTRIES, 1920, 1080)
        ruby_x = [float(x) for x in __import__("re").findall(r"pos\((\d+\.?\d*),", ass)]
        self.assertTrue(ruby_x)
        self.assertGreaterEqual(min(ruby_x), 0)
        self.assertLessEqual(max(ruby_x), 1920)

    def test_long_entry_is_split_inside_build_ass(self):
        """回归：拆句必须在 build_ass 内部做。
        只放在命令行里的话，主脚本调用时不会拆，长句会把整集字号拖小（曾把 60px 压成 57px）。"""
        long_text = "あ" * (fg.limits(1920, 1080)[2] + 5)
        ass, ms, _ = fg.build_ass([(0.0, 10.0, long_text)], 1920, 1080)
        self.assertEqual(ms, fg.limits(1920, 1080)[0])
        self.assertGreater(ass.count(",JP,,0,0,0,,"), 1)        # 拆成了多条 JP 事件


if __name__ == "__main__":
    unittest.main()

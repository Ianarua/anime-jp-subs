# -*- coding: utf-8 -*-
"""日语自己断句（不靠中文字幕轨）的测试。

历史教训（都写成了用例）：
  * 「スポーツ」被切成「ス」「ポーツ」、「いかが|ですか」、「北|くん」；
  * 「…かなっていや」的「いや」被甩在上一行行尾；
  * whisper 的段边界跟真停顿差几百毫秒，照它的时间贴会把断点挪到词中间（用户实测：
    「何かの友達を作ろうとして**俺**は…」没断开、隔了 4 秒的「タ君ですよね」没断开）。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from anime_jp_sub import pipeline as ajs  # noqa: E402


class TestAttachPenalty(unittest.TestCase):
    """切点右边是"附属語"就该罚——那等于把一个文節切开了。"""

    def test_auxiliary_verb_is_penalised(self):
        # いかが|ですか（です = 助動詞）
        self.assertEqual(ajs._attach_penalty("いかが", "ですか"), 4.0)

    def test_suffix_noun_is_penalised(self):
        # 北|くん（くん = 名詞,接尾）
        self.assertEqual(ajs._attach_penalty("北", "くんもしよければ"), 4.0)
        # あり|まして（まし = 助動詞）
        self.assertEqual(ajs._attach_penalty("あり", "まして"), 4.0)

    def test_case_particle_is_penalised_hardest(self):
        self.assertEqual(ajs._attach_penalty("それ", "は"), 6.0)
        self.assertEqual(ajs._attach_penalty("さすがにそれ", "は"), 6.0)

    def test_content_word_is_free(self):
        self.assertEqual(ajs._attach_penalty("ですか", "俺が"), 0.0)
        self.assertEqual(ajs._attach_penalty("どう", "したら"), 0.0)

    def test_quoted_to_is_not_a_particle(self):
        """Janome 把「という」拆成 と(格助詞,引用)+いう，不能当成"下一行从格助詞开头"。

        不拦的话「…です。というか、俺…」会被硬断成「…ですという」「か俺…」（实测）。
        """
        self.assertEqual(ajs._attach_penalty("ことないです", "というか"), 0.0)


class TestLineEdgeJudgement(unittest.TestCase):
    def test_interjection_must_not_end_a_line(self):
        self.assertTrue(ajs._ends_with_sentence_starter("いや"))
        self.assertTrue(ajs._ends_with_sentence_starter("なっていや"))   # 尾巴不是整串
        self.assertFalse(ajs._ends_with_sentence_starter("ですか"))
        self.assertFalse(ajs._ends_with_sentence_starter("かなって"))

    def test_line_may_not_start_with_particle(self):
        self.assertTrue(ajs._bad_line_start("か俺にとっては"))
        self.assertTrue(ajs._bad_line_start("には興味"))
        self.assertFalse(ajs._bad_line_start("というか俺"))     # という 不是助詞
        self.assertFalse(ajs._bad_line_start("俺がはいいつき"))


class TestNearestPause(unittest.TestCase):
    def test_inside_the_pause_counts_as_zero(self):
        pauses = [(1.0, 1.4, 0.4)]
        self.assertEqual(ajs._nearest_pause(1.2, pauses, 0.6), (1.0, 1.4, 0.4))

    def test_too_far_is_none(self):
        self.assertIsNone(ajs._nearest_pause(3.0, [(1.0, 1.4, 0.4)], 0.6))

    def test_picks_the_closest_one(self):
        pauses = [(1.0, 1.4, 0.4), (5.0, 5.3, 0.3)]
        self.assertEqual(ajs._nearest_pause(5.1, pauses, 0.6), (5.0, 5.3, 0.3))


class TestFalseParticle(unittest.TestCase):
    """碎片上下文里 Janome 会把「はい」「でも」拆成 は+いい / で+も，别当成"助詞起句"。"""

    def test_known_false_positives_are_not_penalised(self):
        # 实测：这 20 处误报就是用户说的"开头词被并进上一句"
        self.assertEqual(ajs._attach_penalty("たそこまでは", "でもちゃんと技術力の"), 0.0)
        self.assertEqual(ajs._attach_penalty("私は無敵だ", "じゃあ今度はなりかから"), 0.0)
        self.assertEqual(ajs._attach_penalty("優しいという", "ねえねえ困ったより"), 0.0)

    def test_real_case_particles_still_penalised(self):
        # 真·格助詞/係助詞 开头还是要拦（不能改坏）
        for a, b in (("ですか", "がはい"), ("それ", "はいいですか"),
                     ("どこ", "にいますか"), ("これ", "をください")):
            self.assertEqual(ajs._attach_penalty(a, b), 6.0)


class TestBoundaryRepair(unittest.TestCase):
    """whisper 的段边界修到语言上合法的位置（用户实测的三类毛病）。"""

    def test_all_starters_line_may_end_with_interjection(self):
        """整行都是起句词（「ああはい」）时，允许它以感動詞结尾；
        但「…にはどう」+はい 这种"行尾是感動詞、前面还有别的字"的必须罚。"""
        self.assertTrue(ajs._all_starters("ああはい"))
        self.assertTrue(ajs._all_starters("はい"))
        self.assertFalse(ajs._all_starters("よかったな"))
        self.assertFalse(ajs._all_starters("なのでついていけるか"))

    def test_mid_word_shift_moves_to_word_start(self):
        prev = "ありがとうございます北くんは将"          # whisper 把「将来」切成 将｜来
        self.assertEqual(ajs._mid_word_shift(prev, "来どんな社長になりたいとかって"), len(prev) - 1)
        # 干净的边界不动（用真实数据里的两段）
        self.assertEqual(ajs._mid_word_shift("お渡ししましたよね", "ああはい例えばこちらの方と"), 0)

    def test_trailing_starter_moves_to_next_line(self):
        # 实测：whisper 把下一句开头的「え」「はい」留在上一段末尾
        self.assertEqual(ajs._trailing_starter_start("たどり着いてくれましたえ"), 11)
        self.assertEqual(ajs._trailing_starter_start("よくわかりましたはい"), 8)
        # 整段就是起句词 → 不动（它本来就是独立的一句）
        self.assertEqual(ajs._trailing_starter_start("え"), 0)
        self.assertEqual(ajs._trailing_starter_start("なるほど"), 0)
        # ⚠ 尾巴上的「う」是「思う」的尾巴，不是感動詞（拿 whisper 的碎片词判会踩这个坑）
        self.assertEqual(ajs._trailing_starter_start("しかし楽しめるように体を鍛えて損はないと思う"), 0)

    def test_leading_attach_moves_back_into_previous_line(self):
        self.assertEqual(ajs._leading_attach_len("のよし2人ともいいぞ"), 1)
        self.assertEqual(ajs._leading_attach_len("だよな何とかして"), 1)
        self.assertEqual(ajs._leading_attach_len("だったって今はもういいの"), 3)
        # 这些虽然 Janome 说是助詞，但其实是整词的开头 / 独立一句 → 不动
        self.assertEqual(ajs._leading_attach_len("はい"), 0)
        self.assertEqual(ajs._leading_attach_len("だが子供の頃"), 0)
        self.assertEqual(ajs._leading_attach_len("なあ成香競技大会"), 0)


class TestSegmentRanges(unittest.TestCase):
    """whisper 的段边界 → 吸附到真停顿上，得到每段的真实起止。"""

    @staticmethod
    def _segs(pairs):
        return [(s, e, str(i), [(s, e, str(i))]) for i, (s, e) in enumerate(pairs)]

    def test_boundary_snaps_onto_the_pause(self):
        # 实测开头就是这个形状：段边界 8.83，真停顿 9.09~9.57
        segs = self._segs([(7.0, 8.83), (8.83, 13.2)])
        spans = [(7.0, 9.09), (9.57, 13.25)]
        pauses = [(9.09, 9.57, 0.48)]
        self.assertEqual(ajs._segment_ranges(segs, spans, pauses),
                         [(7.0, 9.09), (9.57, 13.25)])

    def test_pause_4_seconds_away_is_still_used(self):
        # 「気づいてしまった」+4.16 秒停顿 +「タ君ですよね」
        segs = self._segs([(11.0, 13.21), (13.21, 18.5)])
        spans = [(11.04, 13.25), (17.41, 18.72)]
        pauses = [(13.25, 17.41, 4.16)]
        self.assertEqual(ajs._segment_ranges(segs, spans, pauses),
                         [(11.04, 13.25), (17.41, 18.72)])

    def test_no_pause_nearby_keeps_the_original_boundary(self):
        segs = self._segs([(0.0, 1.0), (1.02, 2.0)])
        r = ajs._segment_ranges(segs, [(0.0, 2.0)], [], tol=0.6)
        self.assertAlmostEqual(r[0][1], 1.01, places=6)      # 两句的中间
        self.assertAlmostEqual(r[1][0], 1.01, places=6)

    def test_time_never_goes_backwards(self):
        segs = self._segs([(0.0, 2.0), (0.5, 3.0)])          # 段本身时间重叠
        r = ajs._segment_ranges(segs, [(0.0, 3.0)], [], tol=0.6)
        self.assertGreaterEqual(r[1][0], r[0][1])
        for s, e in r:
            self.assertGreaterEqual(e, s)

    def test_two_boundaries_on_the_same_pause_do_not_invert(self):
        """两条段边界吸到同一条停顿上时，中间那段会被挤成 0 长度——必须夹住。"""
        segs = self._segs([(0.5, 1.0), (1.05, 1.4), (1.45, 2.0)])
        r = ajs._segment_ranges(segs, [(0.5, 1.5), (2.0, 2.5)], [(1.0, 1.5, 0.5)])
        for s, e in r:
            self.assertGreaterEqual(e, s)
        self.assertGreaterEqual(r[2][0], r[1][1])


class TestRetimeWords(unittest.TestCase):
    def test_words_are_laid_out_on_the_snapped_range(self):
        segs = [(0.0, 1.0, "x", [(0.0, 0.5, "あ"), (0.5, 1.0, "い")])]
        out, _cuts, _trusted = ajs._retime_words(segs, [(0.0, 1.0)], [])
        self.assertEqual([w[2] for w in out], ["あ", "い"])
        self.assertAlmostEqual(out[0][0], 0.0, places=6)
        self.assertAlmostEqual(out[-1][1], 1.0, places=6)

    def test_cut_is_put_on_the_pause(self):
        segs = [(0.0, 0.5, "x", [(0.0, 0.5, "あ")]),
                (1.02, 1.5, "y", [(1.02, 1.5, "い")])]
        out, cuts, _trusted = ajs._retime_words(segs, [(0.0, 0.5), (1.0, 1.5)], [(0.5, 1.0, 0.5)])
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(cuts[1], 0.5, places=6)       # 缝 = 真停顿长度
        self.assertAlmostEqual(out[1][0], 1.0, places=6)     # 贴回音频时间

    def test_no_stretch_beyond_limit(self):
        """段里混了音乐/长静音时不许把词无限拉长（实测最夸张差 8 倍）。"""
        segs = [(0.0, 1.0, "x", [(0.0, 0.5, "あ"), (0.5, 1.0, "い")])]
        out, _cuts, _trusted = ajs._retime_words(segs, [(0.0, 100.0)], [])
        self.assertLessEqual(out[-1][1] - out[0][0], 1.0 * 1.6 + 1e-6)

    def test_short_segment_does_not_split_on_its_inner_pause(self):
        """1.02s 的停顿**确实**落在「は|自分」这个词缝上（改版后切点候选来自音频）。

        ⚠ 2026-09 改版：以前切点候选是"whisper 的段界 + 条件注册的段内停顿"，短段内部的
        停顿干脆不算候选；现在切点候选 = 词与词之间的**真实停顿**（词整块落在语音片上），
        所以这里会注册候选。**能不能断**由拼行的代价决定（「行尾是格助詞」+1.5 会拦住它），
        见 TestSegmentLines.test_auxiliary_is_not_separated_from_its_head。
        """
        ws = [(9.57, 10.07, "俺"), (10.07, 10.29, "は"), (10.29, 10.65, "自分"),
              (10.65, 12.0, "自身の問題に気づいてしまった")]
        segs = [(9.5, 13.2, "x", ws)]
        _out, cuts, _trusted = ajs._retime_words(segs, [(9.57, 10.02), (11.04, 13.25)],
                                                 [(10.02, 11.04, 1.02)])
        # 词整块落在语音片上 → 1.02s 的停顿落在**词缝**上（这里落在「俺|は」之间）；
        # 具体落在哪个缝由分组 DP 决定，测试只钉住"缝宽 = 音频实测的停顿"。
        self.assertEqual(list(cuts), [1])
        self.assertAlmostEqual(cuts[1], 1.02, places=2)

    def test_cut_candidates_are_exactly_the_real_gaps(self):
        """切点候选 = 重铺后**词与词之间的真实间隔**（≥ PAUSE_CAND），不多不少。

        ⚠ 2026-09 改版：以前切点按 whisper 的段界注册（段界在哪儿就在哪儿切），用户
        80 条标记里有 23 处判定"断在句子中间"——段界处根本没有停顿，停顿在别的词缝上。
        """
        segs = [(0.0, 1.0, "あい", [(0.0, 0.5, "あ"), (0.5, 1.0, "い")]),
                (1.5, 2.5, "うえ", [(1.5, 2.0, "う"), (2.0, 2.5, "え")])]
        out, cuts, _trusted = ajs._retime_words(segs, [(0.0, 1.0), (1.5, 2.5)],
                                                [(1.0, 1.5, 0.5)])
        for k, gap in cuts.items():
            self.assertGreaterEqual(gap, ajs.PAUSE_CAND)
            self.assertAlmostEqual(out[k][0] - out[k - 1][1], gap, places=2)
        self.assertEqual(sorted(cuts), [2])       # 只有段与段之间那个 0.5s

    def test_no_word_spans_any_pause(self):
        """任何一个词都不许横跨停顿——停顿必须落在词与词之间。

        实测（不是哥们 E11，2026-09 用户标记）：老做法把词按时长比例摊开，0.67s 停顿正好
        落在「それは」**这个词内部**，于是用户想要的断点（…テーブルね|それは私が…）
        在词表里根本不存在，怎么都断不开；反过来「ハルキ」这种词缝上又冒出假切点。
        """
        segs = [(19.0, 22.8, "窓際のテーブルねそれは私が持っていくから",
                 [(19.0, 19.6, "窓際"), (19.6, 20.2, "の"), (20.2, 20.5, "テーブル"),
                  (20.5, 20.7, "ね"), (20.7, 21.3, "それは"), (21.3, 21.7, "私"),
                  (21.7, 22.0, "が"), (22.0, 22.4, "持って"), (22.4, 22.8, "いくから")])]
        spans = [(19.33, 20.67), (21.34, 22.69)]
        pauses = [(20.67, 21.34, 0.67)]
        out, cuts, _trusted = ajs._retime_words(segs, spans, pauses)
        for a, b, w in out:
            for pa, pb, _g in pauses:
                self.assertFalse(a < pa and b > pb, f"{w} 横跨了停顿：{a}-{b}")
        self.assertIn(4, cuts)                    # 「ね|それは」——用户要的断点
        self.assertAlmostEqual(cuts[4], 0.67, places=2)

    def test_no_word_spans_a_long_silence(self):
        """一个词不许横跨长静音（≥ INNER_PAUSE_STRONG）。

        实测（不是哥们 E11，2026-09）：whisper 把隔了 31.5 秒静音的「何が」(1091s) 和
        「あった」(1126s) 并成一段（4 个词），重铺时「が」被铺成 **34.05 秒**、横跨整段静音。
        它接着让"一行不超过 12 秒"的守卫把整集判成无解 → 整集只剩 1 条字幕。
        """
        segs = [(1091.32, 1126.42, "何があった",
                 [(1091.32, 1092.02, "何"), (1092.53, 1094.73, "が"),
                  (1126.18, 1126.30, "あ"), (1126.30, 1126.42, "った")])]
        spans = [(1091.68, 1092.54), (1092.86, 1093.06), (1093.44, 1094.50), (1126.05, 1126.42)]
        pauses = [(1092.544, 1092.864, 0.32), (1093.056, 1093.44, 0.384),
                  (1094.496, 1126.048, 31.552)]
        words, _cuts, _trusted = ajs._retime_words(segs, spans, pauses)
        self.assertEqual([w for _a, _b, w in words], ["何", "が", "あ", "った"])
        for a, b, w in words:
            self.assertFalse(a < 1094.496 and b > 1126.048, f"{w} 横跨了长静音：{a}-{b}")
        # 「が」留在静音**前**那片语音里（夹到语音末尾），「あ」「った」在静音后
        self.assertLessEqual([b for _a, b, w in words if w == "が"][0], 1094.50 + 1e-6)
        self.assertGreaterEqual([a for a, _b, w in words if w == "あ"][0], 1126.05 - 1e-6)


class TestSegmentLines(unittest.TestCase):
    W, H, MAIN = 1920, 1080, 60

    def _lines(self, segs, spans, pauses):
        return ajs.segment_lines(segs, spans, pauses, width=self.W, height=self.H,
                                 main_px=self.MAIN)

    def test_interjection_starts_the_next_line(self):
        """「…かなって」+「いやそんなことないです」：いや 不能留在上一行行尾。

        ⚠ 2026-09 改版：句首的感動詞现在允许**自己成行**（用户在同集把「はい」标记成
        要单独断开的），所以这里不再要求「いやそんなことないです」必须并成一条，
        只要求「いや」不能粘在上一行行尾。测试数据仍按 whisper 的真实形态给
        （它把「いやそんなことないです」算一段，不是把 いや 单独切一段）。
        """
        segs = [(0.00, 0.50, "かなって", [(0.00, 0.50, "かなって")]),
                (1.00, 3.22, "いやそんなことないです",
                 [(1.00, 1.50, "いや"), (2.00, 2.60, "そんな"), (2.60, 2.83, "こと"),
                  (2.83, 3.04, "ない"), (3.04, 3.22, "です")])]
        spans = [(0.0, 0.5), (1.0, 1.5), (2.0, 3.22)]
        pauses = [(0.5, 1.0, 0.5), (1.5, 2.0, 0.5)]
        texts = [t for _s, _e, t in self._lines(segs, spans, pauses)]
        self.assertFalse([t for t in texts if t.endswith("いや") and t != "いや"])
        self.assertIn("いや", texts)                 # 自己成行，且不在上一行行尾

    def test_word_split_across_a_pause_is_avoided(self):
        """「ス」「ポーツ」中间正好有一整段停顿，也不能断（+30 的劈词代价压过停顿奖励）。

        两边都要"长到断了也不难看"，否则用例会靠"太短会被并回去"这条别的规则蒙混过关
        （实测过：把 +30 改成 0，用例照样绿——等于没测）。
        """
        segs = [(0.0, 1.0, "x", [(0.0, 1.0, "ああああああああス")]),
                (1.5, 2.5, "y", [(1.5, 2.5, "ポーツの話をしよう")])]
        texts = [t for _s, _e, t in self._lines(segs, [(0.0, 1.0), (1.5, 2.5)],
                                                [(1.0, 1.5, 0.5)])]
        self.assertFalse(any(t.endswith("ス") for t in texts))
        self.assertTrue(any("スポーツ" in t for t in texts))

    def test_auxiliary_is_not_separated_from_its_head(self):
        """「いかが|ですか」不能断（です 是附属語，+4 压过停顿的 -1.5）。"""
        segs = [(0.0, 0.6, "いかが", [(0.0, 0.6, "いかが")]),
                (1.1, 1.4, "です", [(1.1, 1.4, "です")]),
                (1.4, 1.6, "か", [(1.4, 1.6, "か")]),
                (2.0, 2.4, "俺", [(2.0, 2.4, "俺")]),
                (2.4, 2.6, "が", [(2.4, 2.6, "が")])]
        spans = [(0.0, 0.6), (1.1, 1.6), (2.0, 2.6)]
        pauses = [(0.6, 1.1, 0.5), (1.6, 2.0, 0.4)]
        texts = [t for _s, _e, t in self._lines(segs, spans, pauses)]
        self.assertIn("いかがですか", texts)

    def test_long_pause_between_two_speakers_is_split(self):
        """用户实测 1:17：长静音两侧是两个人说的话，必须切开成两条。

        whisper 把「ああはい」和「例えば…」算成相邻两段，还把时间吸到了前面那个
        0.29s 小停顿上；音频实测中间有 **2.18s** 静音。
        """
        segs = [(75.43, 76.87, "ああはい", [(75.43, 76.19, "ああ"), (76.19, 77.28, "はい")]),
                (77.06, 82.79, "例えばこちらの方と",
                 [(77.06, 77.89, "例えば"), (79.46, 80.03, "こちらの方と")])]
        # 音频实测（这一段的真实 VAD 结果）：两个人在 2.18s 长静音两侧说话
        spans = [(76.19, 76.58), (76.86, 77.28), (79.46, 80.03), (80.48, 81.38)]
        pauses = [(76.58, 76.86, 0.29), (77.28, 79.46, 2.18), (80.03, 80.48, 0.45)]
        texts = [t for _s, _e, t in self._lines(segs, spans, pauses)]
        self.assertIn("ああはい", texts)
        self.assertTrue(any(t.startswith("例えば") for t in texts))
        self.assertFalse(any("ああはい例えば" in t for t in texts))

    def test_long_line_without_pause_is_split_and_never_overflows(self):
        words = [(i * 0.2, i * 0.2 + 0.2, "あ") for i in range(40)]
        lines = self._lines([(0.0, 8.0, "x", words)], [(0.0, 8.0)], [])
        self.assertGreater(len(lines), 1)
        for _s, _e, t in lines:
            self.assertLessEqual(ajs._line_width(t, self.MAIN),
                                 self.W * ajs.LINE_WIDTH_RATIO + 1e-6)

    def test_one_long_word_does_not_kill_the_whole_dp(self):
        """单个词特别长时，守卫必须放行——否则整条 DP 无解、整集塌成 1 行。

        实测（不是哥们 E11，2026-09）：整集 2749 个词只切出 1 行，原因就是这里
        `break` 掉了一个横跨 34 秒的单词。行宽那条守卫早就有 `i < j - 1`，时间这条漏了。
        """
        segs = [(0.0, 13.0, "x", [(0.0, 13.0, "ああああああああああ")]),
                (13.4, 14.0, "y", [(13.4, 14.0, "いいいい")]),
                (14.4, 15.0, "z", [(14.4, 15.0, "うううう")])]
        lines = self._lines(segs, [(0.0, 13.0), (13.4, 14.0), (14.4, 15.0)],
                            [(13.0, 13.4, 0.4), (14.0, 14.4, 0.4)])
        texts = [t for _s, _e, t in lines]
        self.assertGreater(len(lines), 1)
        self.assertIn("いいいい", texts)

    def test_time_never_goes_backwards(self):
        words = [(i * 0.2, i * 0.2 + 0.2, "あ") for i in range(60)]
        segs = [(0.0, 3.0, "a", words[:15]), (3.2, 6.0, "b", words[15:30]),
                (6.2, 12.0, "c", words[30:])]
        lines = self._lines(segs, [(0.0, 3.0), (3.2, 6.0), (6.2, 12.0)],
                            [(3.0, 3.2, 0.2), (6.0, 6.2, 0.2)])
        prev_start = -1.0
        for s, e, _t in lines:
            self.assertGreater(e, s)                 # 结束必须晚于开始
            self.assertGreaterEqual(s, prev_start)   # 起点单调不回退
            prev_start = s

    def test_line_disappears_as_soon_as_the_speech_ends(self):
        """用户 2026-09 定的：说完就消失（HOLD_AFTER=0），别停留 1 秒。"""
        segs = [(0.0, 1.0, "x", [(0.0, 0.5, "あ"), (0.5, 1.0, "い")])]
        lines = self._lines(segs, [(0.0, 1.0)], [])
        self.assertEqual(len(lines), 1)
        _s, e, _t = lines[0]
        self.assertAlmostEqual(e, 1.0, places=6)     # 最后一个词说完就结束

    def test_very_short_line_still_gets_min_show(self):
        """再短也至少显示 MIN_SHOW，不然一闪而过根本看不清。"""
        segs = [(0.0, 0.2, "x", [(0.0, 0.2, "あ")])]
        _s, e, _t = self._lines(segs, [(0.0, 0.2)], [])[0]
        self.assertGreaterEqual(e, 0.6 - 1e-6)       # 写死 0.6：拿常量比会"改坏也测不出来"

    def test_segment_boundary_is_no_longer_a_hard_cut(self):
        """whisper 的段界**不再**是无条件切点（2026-09 改，用户 80 条标记的结论）。

        旧行为：段界上强制断开，哪怕下一段开头在碎片里被 Janome 看成助詞
        （实测「…いかがですか俺が｜はいいつきさんが」其实是「はい、いつきさんが」）。
        新证据：同一集里用户把 23 处段界判成"两句其实是一句"（其中还有 2.05s / 4.00s 这种
        长停顿），所以段界改成"只削弱碎片语法罚分、不强制断"。
        这条用例保住的是**另一半**：段界前那个「が」不能因此把「俺が」劈出来
        （「行尾是格助詞」+1.5 仍在），也就是这里宁可整段并成一条。
        """
        segs = [(0.0, 1.0, "x", [(0.0, 0.4, "ですか"), (0.4, 1.0, "俺が")]),
                (1.4, 2.4, "y", [(1.4, 2.4, "はいいつきさんが")])]
        lines = self._lines(segs, [(0.0, 1.0), (1.4, 2.4)], [(1.0, 1.4, 0.4)])
        texts = [t for _s, _e, t in lines]
        self.assertIn("ですか俺がはいいつきさんが", texts)   # 并成一条，没把「俺が」劈出去
        self.assertNotIn("俺が", texts)                     # 也没切成「…ですか」「俺が|はいいつき…」


if __name__ == "__main__":
    unittest.main()

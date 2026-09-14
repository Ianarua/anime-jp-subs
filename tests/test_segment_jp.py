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
        """段本身还是一句话（词时长不够长）时，段内停顿不当切点。

        实测踩过：不拦的话「俺は自分自身の問題に…」会被切成「俺は」「自分」「自身の…」。
        """
        ws = [(9.57, 10.07, "俺"), (10.07, 10.29, "は"), (10.29, 10.65, "自分"),
              (10.65, 12.0, "自身の問題に気づいてしまった")]
        segs = [(9.5, 13.2, "x", ws)]
        _out, cuts, _trusted = ajs._retime_words(segs, [(9.57, 10.02), (11.04, 13.25)],
                                                 [(10.02, 11.04, 1.02)])
        self.assertEqual(cuts, {})

    def test_mid_word_boundary_is_registered_at_the_word_start(self):
        """whisper 把「将来」切成 将｜来 → 注册的切点要挪到"将"之前（整词归下一句）。"""
        segs = [(0.0, 1.0, "北くんは将", [(0.0, 0.5, "北くん"), (0.5, 1.0, "は将")]),
                (1.5, 2.5, "来どんな社長に", [(1.5, 2.5, "来どんな社長に")])]
        _out, cuts, trusted = ajs._retime_words(segs, [(0.0, 1.0), (1.5, 2.5)],
                                                [(1.0, 1.5, 0.5)])
        self.assertIn(1, cuts)          # 「は将」之前（= 不把 将来 劈开）
        self.assertNotIn(2, cuts)       # 不是原来那个段首（= 「来」之前）
        self.assertIn(1, trusted)

    def test_trailing_starter_boundary_is_registered_before_it(self):
        """上一段末尾的「え」其实是下一句的开头 → 切点要挪到「え」之前。"""
        segs = [(0.0, 1.0, "くれましたえ", [(0.0, 0.6, "くれました"), (0.6, 1.0, "え")]),
                (1.5, 2.5, "今までのお世話係", [(1.5, 2.5, "今までのお世話係")])]
        _out, cuts, trusted = ajs._retime_words(segs, [(0.0, 1.0), (1.5, 2.5)],
                                                [(1.0, 1.5, 0.5)])
        self.assertIn(1, cuts)          # 「え」之前
        self.assertIn(1, trusted)

    def test_trusted_boundary_gets_at_least_a_weak_pause(self):
        """whisper 标了句界、但音频里只有一丁点换气 → 也要按"弱停顿"算，
        否则动态规划宁愿把下一句的开头并进上一行（用户实测抱怨的那种）。"""
        segs = [(0.0, 1.0, "そうですね", [(0.0, 1.0, "そうですね")]),
                (1.05, 2.0, "前にクラスメイトの", [(1.05, 2.0, "前にクラスメイトの")])]
        _out, cuts, _trusted = ajs._retime_words(segs, [(0.0, 1.0), (1.05, 2.0)],
                                                 [(1.0, 1.05, 0.05)])
        self.assertIn(1, cuts)
        self.assertGreaterEqual(cuts[1], ajs.PAUSE_CAND)


class TestSegmentLines(unittest.TestCase):
    W, H, MAIN = 1920, 1080, 60

    def _lines(self, segs, spans, pauses):
        return ajs.segment_lines(segs, spans, pauses, width=self.W, height=self.H,
                                 main_px=self.MAIN)

    def test_interjection_starts_the_next_line(self):
        """「…かなって」+「いやそんなことないです」：いや 不能留在行尾。"""
        segs = [(0.00, 0.50, "かなって", [(0.00, 0.50, "かなって")]),
                (1.00, 1.50, "いや", [(1.00, 1.50, "いや")]),
                (2.00, 3.22, "そんなことないです",
                 [(2.00, 2.60, "そんな"), (2.60, 2.83, "こと"),
                  (2.83, 3.04, "ない"), (3.04, 3.22, "です")])]
        spans = [(0.0, 0.5), (1.0, 1.5), (2.0, 3.22)]
        pauses = [(0.5, 1.0, 0.5), (1.5, 2.0, 0.5)]
        texts = [t for _s, _e, t in self._lines(segs, spans, pauses)]
        self.assertFalse([t for t in texts if t.endswith("いや")])
        self.assertIn("いやそんなことないです", texts)

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

    def test_long_line_without_pause_is_split_and_never_overflows(self):
        words = [(i * 0.2, i * 0.2 + 0.2, "あ") for i in range(40)]
        lines = self._lines([(0.0, 8.0, "x", words)], [(0.0, 8.0)], [])
        self.assertGreater(len(lines), 1)
        for _s, _e, t in lines:
            self.assertLessEqual(ajs._line_width(t, self.MAIN),
                                 self.W * ajs.LINE_WIDTH_RATIO + 1e-6)

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

    def test_segment_boundary_is_trusted(self):
        """whisper 标的句界要真的断开，哪怕下一段开头在碎片里被 Janome 看成助詞。

        实测「…いかがですか俺が｜はいいつきさんが」（其实是「はい、いつきさんが」）：
        旧代码因为碎片里 `はいいつき` 被切成 は+いい+つき，判成"下一行以係助詞开头"→ 整段并进
        上一行。用户看到的就是"开头词被切到上一句里面了"。
        """
        segs = [(0.0, 1.0, "x", [(0.0, 0.4, "ですか"), (0.4, 1.0, "俺が")]),
                (1.4, 2.4, "y", [(1.4, 2.4, "はいいつきさんが")])]
        lines = self._lines(segs, [(0.0, 1.0), (1.4, 2.4)], [(1.0, 1.4, 0.4)])
        texts = [t for _s, _e, t in lines]
        self.assertIn("はいいつきさんが", texts)      # 下一句自己成行
        self.assertFalse([t for t in texts if t.endswith("俺が") is False and "はいいつき" in t
                          and "俺" in t])


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""打分（qa/score）：标记解析 / 标记→词缝映射 / 通过率口径。

这套东西是"改断句算法"的验收标准，口径必须钉死，所以用例写得比较细：
  * 合并标记 = 那个词缝上**不该**有切点；拆分标记 = **该**有切点
  * 允许差一个词缝（±1）
  * baseline（人审的那份）和 words 必须是同一份产物，对不上要立刻报错
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from anime_jp_sub.qa.score import marks as m  # noqa: E402
from anime_jp_sub.qa.score import run as s  # noqa: E402


def _words(*texts):
    """造一串词（每个 0.5s，连着排），只为测映射/打分，不关心真实时间。"""
    out, t = [], 0.0
    for x in texts:
        out.append((t, t + 0.5, x))
        t += 0.5
    return out


class TestParseMarks(unittest.TestCase):
    def test_reads_the_real_format(self):
        text = ("# 不是哥们 E11 字幕评审\n"
                "合并 054-055   [05:23.21 ほうけてないで] + [05:26.13 ハルキ]  → 应该是一条\n"
                "拆分 012 @ 第3字后   [02:29.90 近日に頼まれて]  → 前「近日に」/ 后「頼まれて」\n"
                "漏句 第009条之后 时间「00:38」\n"
                "备注 019  已经不是同一个人说的了\n")
        got = m.parse_marks(text)
        self.assertEqual(got["merges"], [(54, 55)])
        self.assertEqual(got["splits"], [(12, 3)])
        self.assertEqual(got["misses"], 1)
        self.assertEqual(got["notes"], 1)

    def test_empty_text_is_empty_result(self):
        self.assertEqual(m.parse_marks(""), {"merges": [], "splits": [], "misses": 0, "notes": 0})


class TestMapping(unittest.TestCase):
    def test_lines_map_to_word_ranges(self):
        words = _words("はい", "クリーム", "お待たせしました")
        lines = [(0.0, 1.5, "はいクリーム"), (1.5, 2.5, "お待たせしました")]
        rng = m.map_lines_to_words(lines, words)
        self.assertEqual(rng[1], (0, 2))
        self.assertEqual(rng[2], (2, 3))

    def test_mismatch_raises_early(self):
        words = _words("はい", "クリーム")
        with self.assertRaises(AssertionError):
            m.map_lines_to_words([(0.0, 1.0, "ぜんぜん違う")], words)

    def test_seam_time_is_the_middle_of_the_gap(self):
        words = [(0.0, 1.0, "あ"), (1.4, 2.0, "い")]
        self.assertAlmostEqual(m.seam_time(words, 1), 1.2, places=6)

    def test_split_mark_maps_to_the_nearest_word_seam(self):
        """人点的是"第 k 个字之后"，词的边界不一定正好在那儿 → 取最近的那个词缝。"""
        words = _words("近日", "に", "頼まれて")
        lines = [(0.0, 1.5, "近日に頼まれて")]
        rng = m.map_lines_to_words(lines, words)
        marks = {"merges": [], "splits": [(1, 2)], "misses": 0, "notes": 0}
        _wm, want_split = m.mark_seams(marks, rng, words)
        self.assertEqual(want_split, [1])          # 「近日」之后


class TestScore(unittest.TestCase):
    def _case(self, candidate_cuts):
        """words 四个词 → 三条可能的缝（1/2/3）；candidate_cuts 决定切在哪。"""
        words = _words("あ", "い", "う", "え")
        lines = [(0.0, 2.0, "あいうえ")]
        rng = m.map_lines_to_words(lines, words)
        marks = {"merges": [], "splits": [(1, 1)], "misses": 0, "notes": 0}   # 该在缝 1 断
        cand = []
        prev = 0
        for c in sorted(candidate_cuts) + [4]:
            cand.append((words[prev][0], words[c - 1][1], "".join(w[2] for w in words[prev:c])))
            prev = c
        return s.score(lines, words, marks, cand)

    def test_split_satisfied(self):
        r = self._case([1])
        self.assertEqual(r["split"]["ok"], 1)
        self.assertEqual(r["passed"], 1)

    def test_one_seam_off_still_counts(self):
        """差一个词缝算命中（人点的是"这两句该分开"，落在隔壁词缝上阅读体验一样）。"""
        self.assertEqual(self._case([2])["passed"], 1)

    def test_two_seams_off_does_not_count(self):
        self.assertEqual(self._case([3])["passed"], 0)

    def test_merge_mark_fails_when_a_cut_is_there(self):
        """合并标记 = 这个行界上**不该**有切点（行号属于 baseline，所以 baseline 得有这两行）。"""
        words = _words("あ", "い", "う", "え")
        baseline = [(0.0, 0.5, "あ"), (0.5, 2.0, "いうえ")]     # 缝 1 就是"第 1 行末尾"
        marks = {"merges": [(1, 2)], "splits": [], "misses": 0, "notes": 0}
        split_here = [(0.0, 0.5, "あ"), (0.5, 2.0, "いうえ")]    # 还在缝 1 上断开 → 不该
        joined = [(0.0, 2.0, "あいうえ")]                        # 并成一条 → 对
        self.assertEqual(s.score(baseline, words, marks, split_here)["passed"], 0)
        self.assertEqual(s.score(baseline, words, marks, joined)["passed"], 1)


class TestReport(unittest.TestCase):
    def test_report_contains_the_numbers(self):
        words = _words("あ", "い")
        lines = [(0.0, 1.0, "あい")]
        marks = {"merges": [], "splits": [(1, 1)], "misses": 0, "notes": 0}
        r = s.score(lines, words, marks, [(0.0, 0.5, "あ"), (0.5, 1.0, "い")])
        txt = s.report(r, verbose=True)
        self.assertIn("该断（拆分）：  1/1", txt)
        self.assertIn("合计：1/1", txt)


if __name__ == "__main__":
    unittest.main()

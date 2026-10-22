# -*- coding: utf-8 -*-
"""grade_report 的单元测试。运行：python -m unittest discover tests"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from grade_report import (  # noqa: E402
    GradeReportError,
    band_of,
    histogram,
    read_grades,
    score_to_gpa,
    summarize_courses,
    summarize_students,
    weighted_mean,
)

DATA = Path(__file__).resolve().parent.parent / "data" / "sample_grades.csv"


class TestGPA(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(score_to_gpa(100), 4.0)
        self.assertEqual(score_to_gpa(90), 4.0)
        self.assertEqual(score_to_gpa(89), 3.7)
        self.assertEqual(score_to_gpa(60), 1.0)
        self.assertEqual(score_to_gpa(59), 0.0)
        self.assertEqual(score_to_gpa(0), 0.0)

    def test_monotonic(self):
        """分数越高，绩点不应该下降。"""
        gpas = [score_to_gpa(s) for s in range(0, 101)]
        self.assertEqual(gpas, sorted(gpas))


class TestBand(unittest.TestCase):
    def test_band_of(self):
        self.assertEqual(band_of(45), "不及格 <60")
        self.assertEqual(band_of(60), "及格 60-69")
        self.assertEqual(band_of(75), "中等 70-79")
        self.assertEqual(band_of(85), "良好 80-89")
        self.assertEqual(band_of(99), "优秀 90-100")

    def test_every_score_has_a_band(self):
        for s in range(0, 101):
            self.assertTrue(band_of(s))


class TestWeightedMean(unittest.TestCase):
    def test_weighted(self):
        self.assertAlmostEqual(weighted_mean([(90, 5), (80, 3)]), 86.25)

    def test_zero_weight_falls_back(self):
        self.assertAlmostEqual(weighted_mean([(10, 0), (20, 0)]), 15.0)

    def test_empty(self):
        self.assertEqual(weighted_mean([]), 0.0)


class TestRead(unittest.TestCase):
    def test_read_sample(self):
        records = read_grades(DATA)
        self.assertEqual(len(records), 60)  # 12 人 × 5 门
        self.assertEqual(len({r.student_id for r in records}), 12)
        self.assertEqual(len({r.course for r in records}), 5)

    def test_missing_column(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.csv"
            bad.write_text("学号,姓名,课程\n1,甲,数学分析\n", encoding="utf-8")
            with self.assertRaises(GradeReportError):
                read_grades(bad)

    def test_score_out_of_range(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.csv"
            bad.write_text("学号,姓名,课程,学分,成绩\n1,甲,数学分析,3,120\n", encoding="utf-8")
            with self.assertRaises(GradeReportError):
                read_grades(bad)

    def test_gbk_and_bom(self):
        """GBK 编码和 UTF-8 BOM 都要能读。"""
        csv_text = "学号,姓名,课程,学分,成绩\n1,甲,数学分析,3,88\n"
        with tempfile.TemporaryDirectory() as d:
            gbk = Path(d) / "gbk.csv"
            gbk.write_bytes(csv_text.encode("gbk"))
            self.assertEqual(read_grades(gbk)[0].score, 88.0)

            bom = Path(d) / "bom.csv"
            bom.write_bytes(csv_text.encode("utf-8-sig"))
            self.assertEqual(read_grades(bom)[0].course, "数学分析")

    def test_semicolon_delimiter(self):
        csv_text = "学号;姓名;课程;学分;成绩\n1;甲;数学分析;3;88\n"
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "semi.csv"
            p.write_text(csv_text, encoding="utf-8")
            self.assertEqual(read_grades(p)[0].score, 88.0)


class TestSummarize(unittest.TestCase):
    def setUp(self):
        self.records = read_grades(DATA)

    def test_course_stat(self):
        stats = {c.name: c for c in summarize_courses(self.records)}
        ma = stats["数学分析"]
        self.assertEqual(ma.count, 12)
        self.assertAlmostEqual(ma.average, sum(
            r.score for r in self.records if r.course == "数学分析") / 12)
        self.assertTrue(0 <= ma.pass_rate <= 1)
        self.assertEqual(sum(ma.band_counts.values()), 12)

    def test_student_ranking_sorted(self):
        students = summarize_students(self.records)
        avgs = [s.weighted_average for s in students]
        self.assertEqual(avgs, sorted(avgs, reverse=True))
        self.assertEqual(students[0].rank, 1)

    def test_tied_ranks_share_number(self):
        """两人同分时名次应相同，且下一名跳号。"""
        rows = [
            ("1", "甲", "A", 1, 90),
            ("2", "乙", "A", 1, 90),
            ("3", "丙", "A", 1, 70),
        ]
        from grade_report import Record
        recs = [Record(*r) for r in rows]
        ranks = [s.rank for s in summarize_students(recs)]
        self.assertEqual(ranks, [1, 1, 3])

    def test_failed_count(self):
        students = {s.student_id: s for s in summarize_students(self.records)}
        self.assertEqual(students["2024110304"].failed_count, 1)  # 数学分析 58
        self.assertEqual(students["2024110308"].failed_count, 1)  # 高等代数 59
        self.assertEqual(students["2024110305"].failed_count, 0)


class TestRender(unittest.TestCase):
    def test_histogram_contains_all_bands(self):
        from grade_report import BANDS
        text = histogram({
            "不及格 <60": 2, "及格 60-69": 3, "中等 70-79": 5,
            "良好 80-89": 4, "优秀 90-100": 1,
        })
        for _, _, label in BANDS:
            self.assertIn(label, text)

    def test_report_runs(self):
        from grade_report import render_report
        out = render_report(read_grades(DATA), top=3)
        self.assertIn("成绩统计报告", out)
        self.assertIn("数学分析", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)

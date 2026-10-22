#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
grade_report —— 从一张 CSV 成绩单生成统计报告。

零第三方依赖，只用 Python 标准库，Python 3.8+ 可直接运行。

用法：
    python grade_report.py data/sample_grades.csv
    python grade_report.py data/sample_grades.csv --top 5
    python grade_report.py data/sample_grades.csv --student 2024110301
    python grade_report.py data/sample_grades.csv --export result.csv

CSV 需要的列（中英文列名都认，顺序随便）：
    学号 / student_id
    姓名 / name
    课程 / course
    学分 / credit
    成绩 / score
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

__version__ = "1.0.0"

# --------------------------------------------------------------------------
# 列名：把各种写法统一成内部字段名
# --------------------------------------------------------------------------
FIELD_ALIASES: Dict[str, str] = {
    "student_id": "student_id", "学号": "student_id", "编号": "student_id", "id": "student_id",
    "name": "name", "姓名": "name", "学生": "name", "名字": "name",
    "course": "course", "课程": "course", "课程名": "course", "科目": "course",
    "credit": "credit", "学分": "credit",
    "score": "score", "成绩": "score", "分数": "score", "得分": "score",
}
REQUIRED_FIELDS = ("student_id", "name", "course", "credit", "score")

# 分数段：左闭右开，最后一段闭区间
BANDS: Sequence[tuple] = (
    (0, 60, "不及格 <60"),
    (60, 70, "及格 60-69"),
    (70, 80, "中等 70-79"),
    (80, 90, "良好 80-89"),
    (90, 101, "优秀 90-100"),
)


class GradeReportError(Exception):
    """成绩单处理过程中的可预期错误（文件、格式、数据非法）。"""


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Record:
    """一条选课成绩记录。"""
    student_id: str
    name: str
    course: str
    credit: float
    score: float


@dataclass
class CourseStat:
    """一门课的汇总统计。"""
    name: str
    credit: float
    count: int
    average: float
    median: float
    highest: float
    lowest: float
    stdev: float
    pass_rate: float          # 及格率，0~1
    band_counts: Dict[str, int]

    @property
    def failed(self) -> int:
        return self.band_counts.get("不及格 <60", 0)


@dataclass
class StudentStat:
    """一个学生的汇总统计。"""
    student_id: str
    name: str
    credits: float
    weighted_average: float   # 学分加权平均分
    gpa: float                # 4.0 制绩点（加权）
    rank: int = 0
    failed_count: int = 0
    course_count: int = 0


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------
def _normalize_header(fieldnames: Optional[Sequence[str]]) -> Dict[str, str]:
    """把 CSV 表头映射成 {内部字段名: 原始列名}。"""
    if not fieldnames:
        raise GradeReportError("成绩单没有表头，第一行应写列名（学号,姓名,课程,学分,成绩）。")

    mapping: Dict[str, str] = {}
    for col in fieldnames:
        if col is None:
            continue
        key = col.strip().strip('"').lower().replace(" ", "").replace("_", "")
        # 中文不变，英文去掉下划线空格后比较
        internal = FIELD_ALIASES.get(key, FIELD_ALIASES.get(col.strip().lower()))
        if internal and internal not in mapping:
            mapping[internal] = col

    missing = [f for f in REQUIRED_FIELDS if f not in mapping]
    if missing:
        pretty = {"student_id": "学号", "name": "姓名", "course": "课程",
                  "credit": "学分", "score": "成绩"}
        raise GradeReportError(
            "成绩单缺少这些列：" + "、".join(pretty[m] for m in missing)
            + "\n表头至少要有：学号,姓名,课程,学分,成绩"
        )
    return mapping


def _to_float(value: str, field: str, row_no: int) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        raise GradeReportError(f"第 {row_no} 行的「{field}」不是数字：{value!r}") from None


def read_grades(path: Path) -> List[Record]:
    """读 CSV 成绩单，返回记录列表。自动处理 BOM、分隔符和常见编码。"""
    if not path.exists():
        raise GradeReportError(f"找不到文件：{path}")

    raw = path.read_bytes()
    text = _decode(raw)
    # 用 StringIO 喂给 csv，才能先探测分隔符
    sample = text[:4096]
    delimiter = _guess_delimiter(sample)

    reader = csv.DictReader(_strip_lines(text), delimiter=delimiter)
    mapping = _normalize_header(reader.fieldnames)

    records: List[Record] = []
    for row_no, row in enumerate(reader, start=2):
        if row is None:
            continue
        if all((v or "").strip() == "" for v in row.values()):
            continue  # 跳过空行

        course = (row.get(mapping["course"]) or "").strip()
        if not course:
            continue

        records.append(Record(
            student_id=(row.get(mapping["student_id"]) or "").strip(),
            name=(row.get(mapping["name"]) or "").strip(),
            course=course,
            credit=_to_float(row.get(mapping["credit"]), "学分", row_no),
            score=_to_float(row.get(mapping["score"]), "成绩", row_no),
        ))

    if not records:
        raise GradeReportError("成绩单里没有任何有效数据行。")
    _validate(records)
    return records


def _decode(raw: bytes) -> str:
    """依次尝试 utf-8-sig / utf-8 / gbk，谁不报错用谁。"""
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise GradeReportError("文件编码认不出来，请另存为 UTF-8 后再试。")


def _strip_lines(text: str) -> Iterable[str]:
    """去掉完全空的行再交给 csv，避免 DictReader 产生空 dict。"""
    return (line for line in text.splitlines() if line.strip())


def _guess_delimiter(sample: str) -> str:
    """按首行出现次数猜分隔符：逗号 / 分号 / 制表符。"""
    first_line = sample.splitlines()[0] if sample else ""
    counts = {d: first_line.count(d) for d in (",", ";", "\t", "|")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


def _validate(records: Sequence[Record]) -> None:
    for i, r in enumerate(records, start=2):
        if not (0 <= r.score <= 100):
            raise GradeReportError(f"第 {i} 行成绩超出 0~100：{r.score}")
        if r.credit <= 0:
            raise GradeReportError(f"第 {i} 行学分必须为正数：{r.credit}")


# --------------------------------------------------------------------------
# 换算
# --------------------------------------------------------------------------
def score_to_gpa(score: float) -> float:
    """百分制 -> 4.0 制绩点（北大常用的分段算法）。"""
    if score >= 90:
        return 4.0
    if score >= 85:
        return 3.7
    if score >= 82:
        return 3.3
    if score >= 78:
        return 3.0
    if score >= 75:
        return 2.7
    if score >= 72:
        return 2.3
    if score >= 68:
        return 2.0
    if score >= 64:
        return 1.5
    if score >= 60:
        return 1.0
    return 0.0


def band_of(score: float) -> str:
    """分数落在哪个分数段。"""
    for low, high, label in BANDS:
        if low <= score < high:
            return label
    return "优秀 90-100" if score >= 90 else "不及格 <60"


def weighted_mean(pairs: Sequence[tuple]) -> float:
    """pairs = [(数值, 权重), ...]，权重总和为 0 时退化为算术平均。"""
    if not pairs:
        return 0.0
    total_weight = sum(w for _, w in pairs)
    if total_weight == 0:
        return sum(v for v, _ in pairs) / len(pairs)
    return sum(v * w for v, w in pairs) / total_weight


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------
def summarize_courses(records: Sequence[Record]) -> List[CourseStat]:
    """按课程汇总统计，按课程名排序。"""
    grouped: Dict[str, List[Record]] = defaultdict(list)
    for r in records:
        grouped[r.course].append(r)

    stats: List[CourseStat] = []
    for course, items in grouped.items():
        scores = [r.score for r in items]
        counter = Counter(band_of(s) for s in scores)
        stats.append(CourseStat(
            name=course,
            credit=items[0].credit,
            count=len(items),
            average=statistics.fmean(scores),
            median=statistics.median(scores),
            highest=max(scores),
            lowest=min(scores),
            stdev=statistics.stdev(scores) if len(scores) > 1 else 0.0,
            pass_rate=sum(1 for s in scores if s >= 60) / len(scores),
            band_counts={label: counter.get(label, 0) for _, _, label in BANDS},
        ))
    return sorted(stats, key=lambda c: c.name)


def summarize_students(records: Sequence[Record]) -> List[StudentStat]:
    """按学生汇总统计，按加权平均分从高到低排名（同分同名次）。"""
    grouped: Dict[str, List[Record]] = defaultdict(list)
    names: Dict[str, str] = {}
    for r in records:
        grouped[r.student_id].append(r)
        names.setdefault(r.student_id, r.name)

    result: List[StudentStat] = []
    for sid, items in grouped.items():
        credit_sum = sum(r.credit for r in items)
        avg = weighted_mean([(r.score, r.credit) for r in items])
        gpa = weighted_mean([(score_to_gpa(r.score), r.credit) for r in items])
        failed = sum(1 for r in items if r.score < 60)
        result.append(StudentStat(
            student_id=sid,
            name=names.get(sid, ""),
            credits=credit_sum,
            weighted_average=avg,
            gpa=gpa,
            failed_count=failed,
            course_count=len(items),
        ))

    result.sort(key=lambda s: (-s.weighted_average, s.student_id))
    rank = 0
    prev_score: Optional[float] = None
    for i, s in enumerate(result, start=1):
        if prev_score is None or abs(s.weighted_average - prev_score) > 1e-9:
            rank = i
            prev_score = s.weighted_average
        s.rank = rank
    return result


def overall(records: Sequence[Record]) -> Dict[str, object]:
    """整张成绩单的整体概览。"""
    scores = [r.score for r in records]
    students = {r.student_id for r in records}
    courses = {r.course for r in records}
    return {
        "学生数": len(students),
        "课程数": len(courses),
        "记录数": len(records),
        "全体平均分": statistics.fmean(scores),
        "全体中位数": statistics.median(scores),
        "及格率": sum(1 for s in scores if s >= 60) / len(scores),
        "不及格门次": sum(1 for s in scores if s < 60),
    }


# --------------------------------------------------------------------------
# 展示
# --------------------------------------------------------------------------
def disp_width(s: str) -> int:
    """字符串的终端显示宽度：中文/全角算 2，其余算 1。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)


def histogram(band_counts: Dict[str, int], width: int = 28) -> str:
    """把分数段计数画成横向条形图（纯文本，不依赖任何库）。"""
    total = sum(band_counts.values()) or 1
    label_w = max(disp_width(label) for _, _, label in BANDS)
    lines = []
    for _, _, label in BANDS:
        count = band_counts.get(label, 0)
        bar_len = round(count / total * width)
        pad = " " * (label_w - disp_width(label))
        lines.append(f"    {label}{pad} | {'█' * bar_len:<{width}} {count:>3} 人")
    return "\n".join(lines)


def _fmt_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """等宽对齐的纯文本表格。中文按显示宽度算，避免列错位。"""
    cells = [[str(c) for c in row] for row in rows]
    widths = [disp_width(h) for h in headers]
    for row in cells:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], disp_width(c))

    def line(parts: Sequence[str]) -> str:
        out = []
        for p, w in zip(parts, widths):
            out.append(p + " " * (w - disp_width(p)))
        return "  ".join(out).rstrip()

    sep = "  ".join("-" * w for w in widths)
    return "\n".join([line([str(h) for h in headers]), sep] + [line(r) for r in cells])


def render_report(records: Sequence[Record], top: Optional[int] = None) -> str:
    """把整份报告渲染成一段文本。top 限制学生表显示的人数。"""
    ov = overall(records)
    courses = summarize_courses(records)
    students = summarize_students(records)

    out: List[str] = []
    out.append("=" * 62)
    out.append(f"成绩统计报告   grade_report v{__version__}")
    out.append("=" * 62)
    out.append(
        f"学生 {ov['学生数']} 人 · 课程 {ov['课程数']} 门 · 记录 {ov['记录数']} 条 · "
        f"全体平均分 {ov['全体平均分']:.2f} · 及格率 {ov['及格率']:.1%}"
    )
    out.append("")

    out.append("【各课程统计】")
    out.append(_fmt_table(
        ["课程", "学分", "人数", "平均分", "中位数", "最高", "最低", "标准差", "及格率"],
        [[c.name, f"{c.credit:g}", c.count, f"{c.average:.2f}", f"{c.median:.1f}",
          f"{c.highest:g}", f"{c.lowest:g}", f"{c.stdev:.2f}", f"{c.pass_rate:.1%}"]
         for c in courses],
    ))
    out.append("")

    out.append("【全体分数分布】")
    all_bands: Counter = Counter()
    for c in courses:
        all_bands.update(c.band_counts)
    out.append(histogram(dict(all_bands)))
    out.append("")

    shown = students if top is None else students[:top]
    out.append(f"【学生排名】（显示 {len(shown)}/{len(students)} 人）")
    out.append(_fmt_table(
        ["名次", "学号", "姓名", "门数", "总学分", "加权平均分", "GPA", "挂科"],
        [[s.rank, s.student_id, s.name, s.course_count, f"{s.credits:g}",
          f"{s.weighted_average:.2f}", f"{s.gpa:.2f}", s.failed_count] for s in shown],
    ))
    return "\n".join(out)


def render_student(records: Sequence[Record], student_id: str) -> str:
    """单个学生的成绩明细。"""
    own = [r for r in records if r.student_id == student_id]
    if not own:
        raise GradeReportError(f"成绩单里没有学号为 {student_id} 的学生。")

    stat = next(s for s in summarize_students(records) if s.student_id == student_id)
    out = [
        "=" * 50,
        f"{stat.name}（{stat.student_id}）",
        "=" * 50,
        _fmt_table(
            ["课程", "学分", "成绩", "绩点"],
            [[r.course, f"{r.credit:g}", f"{r.score:g}", f"{score_to_gpa(r.score):.1f}"]
             for r in sorted(own, key=lambda r: -r.score)],
        ),
        "",
        f"加权平均分：{stat.weighted_average:.2f}    "
        f"GPA：{stat.gpa:.2f}    "
        f"班级排名：{stat.rank}    挂科门数：{stat.failed_count}",
    ]
    return "\n".join(out)


def export_csv(records: Sequence[Record], dest: Path) -> Path:
    """导出每位学生的汇总结果到 CSV（UTF-8-BOM，Excel 直接双击不乱码）。"""
    students = summarize_students(records)
    with dest.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["名次", "学号", "姓名", "门数", "总学分", "加权平均分", "GPA", "挂科门数"])
        for s in students:
            writer.writerow([s.rank, s.student_id, s.name, s.course_count,
                             s.credits, f"{s.weighted_average:.2f}",
                             f"{s.gpa:.2f}", s.failed_count])
    return dest


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="grade_report",
        description="从 CSV 成绩单生成统计报告（零依赖，纯标准库）",
        epilog="示例：python grade_report.py data/sample_grades.csv --top 5",
    )
    p.add_argument("csv", type=Path, help="成绩单 CSV 文件路径")
    p.add_argument("--top", type=int, metavar="N", help="只显示排名前 N 名的学生")
    p.add_argument("--student", metavar="学号", help="只看某一个学生的成绩明细")
    p.add_argument("--export", type=Path, metavar="路径", help="把学生汇总导出为 CSV")
    p.add_argument("--version", action="version", version=f"grade_report {__version__}")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        records = read_grades(args.csv)

        if args.student:
            print(render_student(records, args.student))
        else:
            print(render_report(records, top=args.top))

        if args.export:
            path = export_csv(records, args.export)
            print(f"\n已导出：{path}")
    except GradeReportError as e:
        print(f"出错：{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

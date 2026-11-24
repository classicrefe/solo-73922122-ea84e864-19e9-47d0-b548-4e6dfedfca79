"""Uploaded-CSV parsing and validation.

Accepted format: UTF-8 text (BOM allowed), one household per row, exactly two
columns — unique household id and an integer score in 0..10.  An optional
header row is auto-detected (it is accepted only when the second column is not
parseable as an integer).  Any invalid row rejects the whole upload, so nothing
is partially written to the database.
"""

from __future__ import annotations

import csv
import io


class CSVValidationError(ValueError):
    pass


def parse_scores(raw_bytes: bytes) -> list[tuple[str, int]]:
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CSVValidationError(f"文件不是有效的 UTF-8 文本：{exc}") from exc

    reader = csv.reader(io.StringIO(text))
    rows: list[tuple[str, int]] = []
    seen: set[str] = set()

    for line_no, raw in enumerate(reader, start=1):
        if not raw or all(cell.strip() == "" for cell in raw):
            continue  # tolerate blank lines
        if len(raw) != 2:
            raise CSVValidationError(
                f"第 {line_no} 行有 {len(raw)} 列，应为 2 列（住户号, 评分）"
            )
        household_id = raw[0].strip()
        score_text = raw[1].strip()
        if household_id == "":
            raise CSVValidationError(f"第 {line_no} 行住户号为空")
        try:
            score = int(score_text)
        except ValueError:
            if line_no == 1:
                # treat the first non-integer row as a header and skip it
                continue
            raise CSVValidationError(
                f"第 {line_no} 行评分 {score_text!r} 不是整数"
            ) from None
        if not 0 <= score <= 10:
            raise CSVValidationError(
                f"第 {line_no} 行评分 {score} 超出 0～10 的范围"
            )
        if household_id in seen:
            raise CSVValidationError(f"住户号 {household_id} 重复出现")
        seen.add(household_id)
        rows.append((household_id, score))

    if not rows:
        raise CSVValidationError("CSV 中没有有效的住户评分记录")
    return rows

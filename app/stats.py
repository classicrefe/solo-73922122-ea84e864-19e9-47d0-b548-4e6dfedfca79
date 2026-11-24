"""Analysis queries.

Everything here reads only the per-round summary tables (``orig_rounds`` /
``corr_rounds``) and scalar ``meta`` totals — no per-household detail.

For each bootstrap round r the weighted mean difference is

    d_r = weighted_sum_corr_r / weight_total_corr_r
          - weighted_sum_orig_r / weight_total_orig_r

Rounds where either side has total weight zero are skipped (undefined mean).
Percentiles use linear interpolation between order statistics (the same
type-7 definition as numpy's default).
"""

from __future__ import annotations

import sqlite3

from .bootstrap import ROUNDS
from .db import META_CORR_N, META_CORR_SUM, META_ORIG_N, META_ORIG_SUM


def percentile_type7(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        raise ValueError("empty input")
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    h = (len(sorted_values) - 1) * (pct / 100.0)
    lo = int(h)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = h - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def _meta_int(conn: sqlite3.Connection, key: str) -> int | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return int(row["value"]) if row is not None else None


def analyze(conn: sqlite3.Connection) -> dict:
    orig_n = _meta_int(conn, META_ORIG_N)
    if orig_n is None:
        return {"status": "no_data", "reason": "尚未上传原表 CSV，请先上传数据。"}

    corr_n = _meta_int(conn, META_CORR_N) or 0
    orig_sum = _meta_int(conn, META_ORIG_SUM) or 0
    corr_sum = _meta_int(conn, META_CORR_SUM) or 0

    result: dict = {
        "status": "ok",
        "orig_n": orig_n,
        "corr_n": corr_n,
        "orig_mean": orig_sum / orig_n if orig_n else None,
        "corr_mean": corr_sum / corr_n if corr_n else None,
        "unweighted_diff": None,
        "rounds_total": ROUNDS,
        "valid_rounds": 0,
        "skipped_rounds": ROUNDS,
        "p025": None,
        "p975": None,
        "reason": None,
    }

    if corr_n == 0:
        result["status"] = "undefined"
        result["reason"] = "更正表已无任何住户，无法计算更正表均分及其差值。"
        return result

    result["unweighted_diff"] = corr_sum / corr_n - orig_sum / orig_n

    rows = conn.execute(
        """
        SELECT o.round_index       AS round_index,
               o.weight_total      AS ow,
               o.weighted_sum      AS ows,
               c.weight_total      AS cw,
               c.weighted_sum      AS cws
        FROM orig_rounds o
        JOIN corr_rounds c ON c.round_index = o.round_index
        ORDER BY o.round_index
        """
    ).fetchall()

    diffs: list[float] = []
    skipped = 0
    for row in rows:
        if row["ow"] == 0 or row["cw"] == 0:
            skipped += 1  # side with zero total weight -> mean undefined
            continue
        diffs.append(row["cws"] / row["cw"] - row["ows"] / row["ow"])

    result["valid_rounds"] = len(diffs)
    result["skipped_rounds"] = skipped

    if not diffs:
        result["status"] = "undefined"
        result["reason"] = (
            f"全部 {ROUNDS} 轮自助抽样中都至少有一侧的权重合计为零，"
            "没有可用于计算分位的有效轮次。"
        )
        return result

    diffs.sort()
    result["p025"] = percentile_type7(diffs, 2.5)
    result["p975"] = percentile_type7(diffs, 97.5)
    return result

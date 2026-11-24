"""SQLite persistence.

Schema
------
original_scores     original table, inserted once per upload and never
                    modified afterwards (requirement: kept as-is).
corrected_scores    working table; starts as a copy of the original and is
                    edited one household at a time.
orig_rounds / corr_rounds
                    per-round weighted summaries (sum of weight, sum of
                    weighted score) for the two tables.  Analysis reads only
                    these summary tables plus the ``meta`` totals, never any
                    per-household detail.
meta                 scalar key/value bookkeeping.

Every correction recomputes only the affected household's 1000 round rows and
commits corrected_scores together with corr_rounds in a single transaction, so
a failure leaves the previous correction and the previous summary in place.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .bootstrap import ROUNDS, household_weights

SCHEMA = """
CREATE TABLE IF NOT EXISTS original_scores (
    household_id TEXT PRIMARY KEY,
    score        INTEGER NOT NULL CHECK (score BETWEEN 0 AND 10)
);
CREATE TABLE IF NOT EXISTS corrected_scores (
    household_id TEXT PRIMARY KEY,
    score        INTEGER NOT NULL CHECK (score BETWEEN 0 AND 10)
);
CREATE TABLE IF NOT EXISTS orig_rounds (
    round_index      INTEGER PRIMARY KEY CHECK (round_index BETWEEN 0 AND 999),
    weight_total     INTEGER NOT NULL,
    weighted_sum     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS corr_rounds (
    round_index      INTEGER PRIMARY KEY CHECK (round_index BETWEEN 0 AND 999),
    weight_total     INTEGER NOT NULL,
    weighted_sum     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

META_ORIG_SUM = "orig_score_sum"
META_ORIG_N = "orig_count"
META_CORR_SUM = "corr_score_sum"
META_CORR_N = "corr_count"
META_UPLOADED_AT = "uploaded_at"


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _round_rows(household_id: str, score: int) -> list[tuple[int, int, int]]:
    weights = household_weights(household_id)
    return [(r, w, w * score) for r, w in enumerate(weights)]


def _set_meta(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def replace_original(conn: sqlite3.Connection, rows: list[tuple[str, int]],
                     uploaded_at: str) -> None:
    """Atomically replace the whole dataset.

    The original table, the corrected copy, both round summaries and the
    scalar meta values are rebuilt in one transaction.
    """
    rounds_zero = [(r, 0, 0) for r in range(ROUNDS)]
    with conn:  # COMMIT on success, ROLLBACK on any exception
        conn.execute("DELETE FROM original_scores")
        conn.execute("DELETE FROM corrected_scores")
        conn.execute("DELETE FROM orig_rounds")
        conn.execute("DELETE FROM corr_rounds")
        conn.executemany(
            "INSERT INTO original_scores(household_id, score) VALUES(?, ?)", rows
        )
        conn.executemany(
            "INSERT INTO corrected_scores(household_id, score) VALUES(?, ?)", rows
        )
        conn.executemany(
            "INSERT INTO orig_rounds(round_index, weight_total, weighted_sum) "
            "VALUES(?, ?, ?)",
            rounds_zero,
        )
        conn.executemany(
            "INSERT INTO corr_rounds(round_index, weight_total, weighted_sum) "
            "VALUES(?, ?, ?)",
            rounds_zero,
        )
        total_score = sum(score for _, score in rows)
        _set_meta(conn, META_ORIG_SUM, total_score)
        _set_meta(conn, META_ORIG_N, len(rows))
        _set_meta(conn, META_CORR_SUM, total_score)
        _set_meta(conn, META_CORR_N, len(rows))
        _set_meta(conn, META_UPLOADED_AT, uploaded_at)
        for household_id, score in rows:
            household_rounds = _round_rows(household_id, score)
            conn.executemany(
                "INSERT INTO orig_rounds(round_index, weight_total, weighted_sum) "
                "VALUES(?, ?, ?) "
                "ON CONFLICT(round_index) DO UPDATE SET "
                "weight_total = weight_total + excluded.weight_total, "
                "weighted_sum = weighted_sum + excluded.weighted_sum",
                household_rounds,
            )
            conn.executemany(
                "INSERT INTO corr_rounds(round_index, weight_total, weighted_sum) "
                "VALUES(?, ?, ?) "
                "ON CONFLICT(round_index) DO UPDATE SET "
                "weight_total = weight_total + excluded.weight_total, "
                "weighted_sum = weighted_sum + excluded.weighted_sum",
                household_rounds,
            )


def apply_correction(conn: sqlite3.Connection, household_id: str,
                     action: str, new_score: int | None) -> None:
    """Apply one add / score-change / removal, atomically with its summary.

    Only the edited household's per-round contribution is recomputed; all
    other households' weights and contributions stay untouched.
    """
    with conn:  # read and write happen in one transaction
        existing = conn.execute(
            "SELECT score FROM corrected_scores WHERE household_id = ?",
            (household_id,),
        ).fetchone()
        if action == "remove":
            if existing is None:
                raise ValueError(f"住户 {household_id} 不在更正表中，无法移除")
            old_score = existing["score"]
            old_rows = _round_rows(household_id, old_score)
            conn.execute(
                "DELETE FROM corrected_scores WHERE household_id = ?",
                (household_id,),
            )
            conn.executemany(
                "UPDATE corr_rounds SET "
                "weight_total = weight_total - ?, "
                "weighted_sum = weighted_sum - ? "
                "WHERE round_index = ?",
                [(w, ws, r) for r, w, ws in old_rows],
            )
            conn.execute(
                f"UPDATE meta SET value = CAST(value AS INTEGER) - ? "
                f"WHERE key = '{META_CORR_SUM}'",
                (old_score,),
            )
            conn.execute(
                f"UPDATE meta SET value = CAST(value AS INTEGER) - 1 "
                f"WHERE key = '{META_CORR_N}'"
            )
            return

        if action not in ("add", "update"):
            raise ValueError(f"未知操作: {action}")
        if new_score is None or not 0 <= new_score <= 10:
            raise ValueError("评分必须是 0～10 的整数")

        new_rows = _round_rows(household_id, new_score)
        if existing is None:
            if action == "update":
                raise ValueError(
                    f"住户 {household_id} 不在更正表中，无法改分；如需新增请选择补录"
                )
            conn.execute(
                "INSERT INTO corrected_scores(household_id, score) VALUES(?, ?)",
                (household_id, new_score),
            )
            conn.executemany(
                "INSERT INTO corr_rounds(round_index, weight_total, weighted_sum) "
                "VALUES(?, ?, ?) "
                "ON CONFLICT(round_index) DO UPDATE SET "
                "weight_total = weight_total + excluded.weight_total, "
                "weighted_sum = weighted_sum + excluded.weighted_sum",
                new_rows,
            )
            conn.execute(
                f"UPDATE meta SET value = CAST(value AS INTEGER) + ? "
                f"WHERE key = '{META_CORR_SUM}'",
                (new_score,),
            )
            conn.execute(
                f"UPDATE meta SET value = CAST(value AS INTEGER) + 1 "
                f"WHERE key = '{META_CORR_N}'"
            )
        else:
            if action == "add":
                raise ValueError(
                    f"住户 {household_id} 已在更正表中（评分 {existing['score']}），"
                    "如需修改请选择改分"
                )
            old_score = existing["score"]
            if old_score == new_score:
                return  # nothing to do; previous result stays in place
            old_rows = _round_rows(household_id, old_score)
            conn.execute(
                "UPDATE corrected_scores SET score = ? WHERE household_id = ?",
                (new_score, household_id),
            )
            conn.executemany(
                "UPDATE corr_rounds SET "
                "weight_total = weight_total - ?, "
                "weighted_sum = weighted_sum - ? "
                "WHERE round_index = ?",
                [(w, ws, r) for r, w, ws in old_rows],
            )
            conn.executemany(
                "UPDATE corr_rounds SET "
                "weight_total = weight_total + ?, "
                "weighted_sum = weighted_sum + ? "
                "WHERE round_index = ?",
                [(w, ws, r) for r, w, ws in new_rows],
            )
            conn.execute(
                f"UPDATE meta SET value = CAST(value AS INTEGER) + ? "
                f"WHERE key = '{META_CORR_SUM}'",
                (new_score - old_score,),
            )
            # count unchanged on score update


def reset_corrected(conn: sqlite3.Connection) -> None:
    """Reset the correction table (and its summary) back to the original."""
    with conn:
        orig = conn.execute(
            "SELECT household_id, score FROM original_scores"
        ).fetchall()
        conn.execute("DELETE FROM corrected_scores")
        conn.execute("DELETE FROM corr_rounds")
        conn.executemany(
            "INSERT INTO corrected_scores(household_id, score) VALUES(?, ?)",
            [(r["household_id"], r["score"]) for r in orig],
        )
        conn.executemany(
            "INSERT INTO corr_rounds(round_index, weight_total, weighted_sum) "
            "VALUES(?, ?, ?)",
            [(r, 0, 0) for r in range(ROUNDS)],
        )
        for r in orig:
            conn.executemany(
                "INSERT INTO corr_rounds(round_index, weight_total, weighted_sum) "
                "VALUES(?, ?, ?) "
                "ON CONFLICT(round_index) DO UPDATE SET "
                "weight_total = weight_total + excluded.weight_total, "
                "weighted_sum = weighted_sum + excluded.weighted_sum",
                _round_rows(r["household_id"], r["score"]),
            )
        total = conn.execute(
            f"SELECT value FROM meta WHERE key = '{META_ORIG_SUM}'"
        ).fetchone()
        count = conn.execute(
            f"SELECT value FROM meta WHERE key = '{META_ORIG_N}'"
        ).fetchone()
        _set_meta(conn, META_CORR_SUM, total["value"] if total else 0)
        _set_meta(conn, META_CORR_N, count["value"] if count else 0)


def get_household(conn: sqlite3.Connection, household_id: str) -> dict | None:
    """Single-household lookup; no other household's detail is read."""
    o = conn.execute(
        "SELECT score FROM original_scores WHERE household_id = ?",
        (household_id,),
    ).fetchone()
    c = conn.execute(
        "SELECT score FROM corrected_scores WHERE household_id = ?",
        (household_id,),
    ).fetchone()
    if o is None and c is None:
        return None
    return {
        "household_id": household_id,
        "original_score": o["score"] if o else None,
        "corrected_score": c["score"] if c else None,
    }


def has_dataset(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM meta WHERE key = '{META_ORIG_N}'"
    ).fetchone()
    return row is not None

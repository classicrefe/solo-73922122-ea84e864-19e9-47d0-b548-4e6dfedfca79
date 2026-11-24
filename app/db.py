"""SQLite persistence.

Schema
------
original_scores     original table, inserted once per upload and never
                    modified by corrections.
corrected_scores    working table; starts as a copy of the original and is
                    edited one household at a time.
orig_rounds / corr_rounds
                    per-round weighted summaries (sum of weight, sum of
                    weighted score) for the two tables.  Analysis reads only
                    these summary tables plus the ``meta`` totals, never any
                    per-household detail.
meta                 scalar key/value bookkeeping, including the upload
                    baseline (content fingerprint + generation) used to
                    detect a concurrent re-upload.

Incremental re-upload
---------------------
A re-upload is planned against the submitted original table and its summary:
only households that were added, removed or whose score changed have their
1000 bootstrap weights generated.  The stored per-round summary is then
updated by the delta of exactly those households.  Because weights are a
deterministic function of (seed, round, household id), a table whose rows
were merely re-ordered yields a delta of zero and therefore generates no
weights at all.

The plan is computed outside any database transaction, so queries and
per-household corrections keep working while it runs.  Writing the original
table, the corrected copy and both summaries happens in one
``BEGIN IMMEDIATE`` transaction.  Inside that transaction the stored baseline
generation/fingerprint is compared with the one submitted by the page; if
another original upload committed in the meantime the transaction is rolled
back and :class:`UploadConflict` is raised, even when both uploads carried
identical content.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
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
# Baseline bookkeeping for incremental re-uploads.
META_ORIG_FP = "orig_fingerprint"
META_UPLOAD_GEN = "upload_generation"
META_LAST_COUNT = "last_recomputed_count"
META_LAST_MODE = "last_upload_mode"
META_LAST_AT = "last_upload_at"
META_ORIG_WTOTAL = "orig_weight_total"
META_ORIG_WSUM = "orig_weighted_sum"


class UploadConflict(RuntimeError):
    """Another original-table upload committed after this one read its baseline."""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


_REQUIRED_TABLES = ("original_scores", "corrected_scores", "orig_rounds",
                    "corr_rounds", "meta")


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema once; stay a read-only no-op on later requests.

    Every request calls this, so the fast path must not take a write lock —
    that keeps queries available while an upload is committing.
    """
    present = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if all(table in present for table in _REQUIRED_TABLES):
        return
    conn.executescript(SCHEMA)
    conn.commit()


def enable_wal(conn: sqlite3.Connection) -> None:
    """Switch the database file to WAL (once, at application start).

    WAL lets readers keep querying while an upload transaction writes, and also
    upgrades databases created by older versions.  Must run outside an open
    transaction; losing a race between workers is harmless.
    """
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.OperationalError:
        pass


@contextmanager
def _write_txn(conn: sqlite3.Connection):
    """Serialise writers with ``BEGIN IMMEDIATE`` and commit on success.

    If the caller already opened a transaction, join it instead of committing
    independently (allows the helpers to be composed).
    """
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def fingerprint_rows(rows) -> str:
    """Order-independent content fingerprint of a (household, score) table."""
    digest = hashlib.sha256()
    for household_id, score in sorted(rows):
        digest.update(household_id.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(score).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def empty_fingerprint() -> str:
    """Fingerprint of an empty original table (the blank-page baseline)."""
    return fingerprint_rows(())


def _round_rows(household_id: str, score: int) -> list[tuple[int, int, int]]:
    weights = household_weights(household_id)
    return [(r, w, w * score) for r, w in enumerate(weights)]


def _set_meta(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def _meta_map(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _ensure_baseline_tx(conn: sqlite3.Connection) -> dict | None:
    """Return the stored upload baseline, backfilling it for legacy databases.

    Must be called inside a write transaction.  Existing databases created
    before incremental uploads have no fingerprint/generation; they are
    upgraded in place using their current original table and summary.
    """
    meta = _meta_map(conn)
    if META_ORIG_N not in meta:
        return None

    fingerprint = meta.get(META_ORIG_FP)
    generation = _int_or_none(meta.get(META_UPLOAD_GEN))
    if fingerprint is None:
        rows = conn.execute(
            "SELECT household_id, score FROM original_scores"
        ).fetchall()
        fingerprint = fingerprint_rows(
            (r["household_id"], r["score"]) for r in rows
        )
        _set_meta(conn, META_ORIG_FP, fingerprint)
    if generation is None:
        generation = 1
        _set_meta(conn, META_UPLOAD_GEN, generation)
    if META_LAST_COUNT not in meta:
        count = conn.execute(
            "SELECT COUNT(*) AS k FROM original_scores"
        ).fetchone()["k"]
        _set_meta(conn, META_LAST_COUNT, count)
    if META_LAST_MODE not in meta:
        _set_meta(conn, META_LAST_MODE, "legacy")
    if META_LAST_AT not in meta and META_UPLOADED_AT in meta:
        _set_meta(conn, META_LAST_AT, meta[META_UPLOADED_AT])
    if META_ORIG_WTOTAL not in meta or META_ORIG_WSUM not in meta:
        sums = conn.execute(
            "SELECT COALESCE(SUM(weight_total), 0) AS w, "
            "COALESCE(SUM(weighted_sum), 0) AS ws FROM orig_rounds"
        ).fetchone()
        _set_meta(conn, META_ORIG_WTOTAL, sums["w"])
        _set_meta(conn, META_ORIG_WSUM, sums["ws"])
    return {"fingerprint": fingerprint, "generation": generation}


def get_upload_status(conn: sqlite3.Connection) -> dict | None:
    """Baseline plus last-upload counters for the page (reads meta only)."""
    if not has_dataset(conn):
        return None
    meta = _meta_map(conn)
    if (
        meta.get(META_ORIG_FP) is None
        or _int_or_none(meta.get(META_UPLOAD_GEN)) is None
    ):
        with _write_txn(conn):
            _ensure_baseline_tx(conn)
        meta = _meta_map(conn)
    return {
        "fingerprint": meta.get(META_ORIG_FP),
        "generation": _int_or_none(meta.get(META_UPLOAD_GEN)),
        "orig_n": _int_or_none(meta.get(META_ORIG_N)),
        "last_mode": meta.get(META_LAST_MODE, "legacy"),
        "last_recomputed_count": _int_or_none(meta.get(META_LAST_COUNT)),
        "last_upload_at": meta.get(META_LAST_AT) or meta.get(META_UPLOADED_AT),
    }


def load_upload_baseline(conn: sqlite3.Connection) -> dict | None:
    """Snapshot the committed original table and its baseline metadata.

    Returns ``None`` when no dataset exists yet (first upload).
    """
    status = get_upload_status(conn)
    if status is None:
        return None
    started = False
    if not conn.in_transaction:
        conn.execute("BEGIN")
        started = True
    try:
        meta = _meta_map(conn)
        rows = [
            (r["household_id"], r["score"])
            for r in conn.execute(
                "SELECT household_id, score FROM original_scores ORDER BY household_id"
            )
        ]
    finally:
        if started:
            conn.rollback()
    status.update(
        {
            "fingerprint": meta.get(META_ORIG_FP),
            "generation": _int_or_none(meta.get(META_UPLOAD_GEN)),
            "rows": rows,
        }
    )
    return status


def build_upload_plan(new_rows: list[tuple[str, int]],
                      baseline_rows: list[tuple[str, int]],
                      force_full: bool = False) -> dict:
    """Compute which households need weights and the per-round summary delta.

    No database access happens here, so it is safe (and intended) to run while
    other requests read or correct the dataset.  Only added, removed or
    score-changed households have weights generated; re-ordering rows alone
    produces ``changed_count == 0`` and an all-zero delta.  ``force_full``
    recomputes every household (repair path / explicit "recompute all").
    """
    new_map = dict(new_rows)
    if len(new_map) != len(new_rows):
        raise ValueError("住户号重复，无法生成上传计划")
    old_map = dict(baseline_rows)
    baseline_fingerprint = fingerprint_rows(old_map.items())

    if force_full:
        changed = sorted(new_map)
        delta_base: dict[str, int] = {}  # delta against an empty summary
    else:
        changed = sorted(
            [hid for hid, score in new_map.items() if old_map.get(hid) != score]
            + [hid for hid in old_map if hid not in new_map]
        )
        delta_base = old_map

    delta_weight = [0] * ROUNDS
    delta_sum = [0] * ROUNDS
    for household_id in changed:
        weights = household_weights(household_id)
        if household_id in new_map and household_id in delta_base:
            score_delta = new_map[household_id] - delta_base[household_id]
            for rnd, weight in enumerate(weights):
                delta_sum[rnd] += weight * score_delta
        elif household_id in new_map:  # newly added
            score = new_map[household_id]
            for rnd, weight in enumerate(weights):
                delta_weight[rnd] += weight
                delta_sum[rnd] += weight * score
        else:  # removed
            score = delta_base[household_id]
            for rnd, weight in enumerate(weights):
                delta_weight[rnd] -= weight
                delta_sum[rnd] -= weight * score

    return {
        "changed_count": len(changed),
        "baseline_fingerprint": baseline_fingerprint,
        "delta": [
            (delta_weight[rnd], delta_sum[rnd], rnd) for rnd in range(ROUNDS)
        ],
        "new_count": len(new_map),
        "new_score_sum": sum(new_map.values()),
        "delta_weight_total": sum(delta_weight),
        "delta_sum_total": sum(delta_sum),
        "full": not old_map,
        "reset_rounds": force_full,
    }


def _write_dataset(
    conn: sqlite3.Connection,
    rows: list[tuple[str, int]],
    uploaded_at: str,
    delta_rows: list[tuple[int, int, int]],
    *,
    reset_rounds: bool,
    mode: str,
    recomputed_count: int,
    fingerprint: str,
    total_score: int,
    generation: int,
) -> None:
    """Write a complete dataset inside the caller's transaction."""
    conn.execute("DELETE FROM original_scores")
    conn.execute("DELETE FROM corrected_scores")
    conn.executemany(
        "INSERT INTO original_scores(household_id, score) VALUES(?, ?)", rows
    )
    conn.executemany(
        "INSERT INTO corrected_scores(household_id, score) VALUES(?, ?)", rows
    )

    if reset_rounds:
        conn.execute("DELETE FROM orig_rounds")
        conn.executemany(
            "INSERT INTO orig_rounds(round_index, weight_total, weighted_sum) "
            "VALUES(?, 0, 0)",
            [(rnd,) for rnd in range(ROUNDS)],
        )
    # Apply only the changed households' contribution to the committed summary.
    conn.executemany(
        "UPDATE orig_rounds SET "
        "weight_total = weight_total + ?, "
        "weighted_sum = weighted_sum + ? "
        "WHERE round_index = ?",
        delta_rows,
    )
    # The corrected table is re-initialised from the new original table.
    conn.execute("DELETE FROM corr_rounds")
    conn.execute(
        "INSERT INTO corr_rounds(round_index, weight_total, weighted_sum) "
        "SELECT round_index, weight_total, weighted_sum FROM orig_rounds"
    )

    count = len(rows)
    _set_meta(conn, META_ORIG_SUM, total_score)
    _set_meta(conn, META_ORIG_N, count)
    _set_meta(conn, META_CORR_SUM, total_score)
    _set_meta(conn, META_CORR_N, count)
    _set_meta(conn, META_UPLOADED_AT, uploaded_at)
    _set_meta(conn, META_ORIG_FP, fingerprint)
    _set_meta(conn, META_UPLOAD_GEN, generation)
    _set_meta(conn, META_LAST_COUNT, recomputed_count)
    _set_meta(conn, META_LAST_MODE, mode)
    _set_meta(conn, META_LAST_AT, uploaded_at)
    sums = conn.execute(
        "SELECT COALESCE(SUM(weight_total), 0) AS w, "
        "COALESCE(SUM(weighted_sum), 0) AS ws FROM orig_rounds"
    ).fetchone()
    _set_meta(conn, META_ORIG_WTOTAL, sums["w"])
    _set_meta(conn, META_ORIG_WSUM, sums["ws"])


def replace_original(conn: sqlite3.Connection, rows: list[tuple[str, int]],
                     uploaded_at: str) -> dict:
    """Unconditionally replace the whole dataset (full computation).

    Kept for first-time ingestion and administrative resets.  The original
    table, the corrected copy, both round summaries and the scalar meta
    values are rebuilt in one transaction.
    """
    plan = build_upload_plan(rows, [])
    with _write_txn(conn):
        current = _ensure_baseline_tx(conn)
        generation = (current["generation"] + 1) if current else 1
        _write_dataset(
            conn, rows, uploaded_at, plan["delta"],
            reset_rounds=True, mode="full",
            recomputed_count=len(rows),
            fingerprint=fingerprint_rows(rows),
            total_score=plan["new_score_sum"], generation=generation,
        )
    return {
        "mode": "full",
        "first_upload": True,
        "recomputed_count": len(rows),
        "generation": generation,
    }


def reupload_original(
    conn: sqlite3.Connection,
    rows: list[tuple[str, int]],
    uploaded_at: str,
    base_fingerprint: str | None,
    base_generation: int | None,
    plan: dict,
) -> dict:
    """Apply a planned re-upload unless another upload changed the baseline.

    ``base_fingerprint``/``base_generation`` are the values the page showed
    when the CSV was picked.  If the database has moved on since then,
    :class:`UploadConflict` is raised and nothing is written — even if the
    other upload carried byte-identical content.
    """
    with _write_txn(conn):
        current = _ensure_baseline_tx(conn)

        if base_generation is None:
            if current is not None or base_fingerprint is not None:
                raise UploadConflict(
                    "数据库已有原表，另一笔上传可能已先入库"
                )
            expected_fingerprint = fingerprint_rows([])
        else:
            if current is None:
                raise UploadConflict(
                    "数据库中的原表已不存在，基准已变化"
                )
            if (
                current["generation"] != base_generation
                or current["fingerprint"] != base_fingerprint
            ):
                raise UploadConflict(
                    "基准已被另一笔原表上传更新"
                )
            expected_fingerprint = current["fingerprint"]

        if plan.get("baseline_fingerprint") != expected_fingerprint:
            raise UploadConflict("上传计划基于的原表与当前基准不一致")
        if plan.get("new_count") != len(rows):
            raise ValueError("上传计划与提交的 CSV 户数不一致")

        if current is not None and not plan.get("reset_rounds"):
            # Consistency guard: the increment is applied to the stored
            # summary, so refuse if the committed summary no longer agrees with
            # the committed original table / totals (e.g. a hand-edited or
            # partially restored database).
            table_n, table_sum = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(score), 0) AS s "
                "FROM original_scores"
            ).fetchone()
            meta = _meta_map(conn)
            if (
                table_n != _int_or_none(meta.get(META_ORIG_N))
                or table_sum != _int_or_none(meta.get(META_ORIG_SUM))
            ):
                raise RuntimeError(
                    "原表与元数据不一致，请勾选「强制全量重算」重新上传"
                )
            summary = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(weight_total), 0) AS w, "
                "COALESCE(SUM(weighted_sum), 0) AS ws FROM orig_rounds"
            ).fetchone()
            if (
                summary["n"] != ROUNDS
                or summary["w"] != _int_or_none(meta.get(META_ORIG_WTOTAL))
                or summary["ws"] != _int_or_none(meta.get(META_ORIG_WSUM))
            ):
                raise RuntimeError(
                    "原表轮次汇总与基准不一致，请勾选「强制全量重算」重新上传"
                )

        full = current is None
        reset_rounds = full or bool(plan.get("reset_rounds"))
        generation = 1 if full else current["generation"] + 1
        if reset_rounds and not full:
            mode = "full"
        else:
            mode = "full" if full else "incremental"
        _write_dataset(
            conn, rows, uploaded_at, plan["delta"],
            reset_rounds=reset_rounds,
            mode=mode,
            recomputed_count=plan["changed_count"],
            fingerprint=fingerprint_rows(rows),
            total_score=plan["new_score_sum"],
            generation=generation,
        )
    return {
        "mode": mode,
        "first_upload": full,
        "recomputed_count": plan["changed_count"],
        "generation": generation,
        "household_count": len(rows),
    }


def apply_correction(conn: sqlite3.Connection, household_id: str,
                     action: str, new_score: int | None) -> None:
    """Apply one add / score-change / removal, atomically with its summary.

    Only the edited household's per-round contribution is recomputed; all
    other households' weights and contributions stay untouched.  Uses
    ``BEGIN IMMEDIATE`` so corrections and upload commits serialise cleanly.
    """
    with _write_txn(conn):  # read and write happen in one transaction
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
    with _write_txn(conn):
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
            "VALUES(?, 0, 0)",
            [(r,) for r in range(ROUNDS)],
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

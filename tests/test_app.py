"""End-to-end tests: CSV upload, corrections, summaries, rollback, HTTP flow."""

import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.bootstrap import household_weights  # noqa: E402
from app.csv_io import CSVValidationError, parse_scores  # noqa: E402
from app.db import (  # noqa: E402
    META_CORR_N,
    META_CORR_SUM,
    META_ORIG_N,
    META_ORIG_SUM,
    UploadConflict,
    apply_correction,
    build_upload_plan,
    connect,
    get_household,
    get_upload_status,
    init_db,
    load_upload_baseline,
    replace_original,
    reupload_original,
    reset_corrected,
)
from app.stats import analyze  # noqa: E402


def csv_bytes(lines, header=True):
    out = "住户号,评分\n" if header else ""
    out += "\n".join(f"{h},{s}" for h, s in lines)
    return out.encode("utf-8")


class CsvParseTests(unittest.TestCase):
    def test_valid_with_and_without_header(self):
        rows = parse_scores(csv_bytes([("A", 0), ("B", 10)], header=True))
        self.assertEqual(rows, [("A", 0), ("B", 10)])
        rows2 = parse_scores(csv_bytes([("A", 0), ("B", 10)], header=False))
        self.assertEqual(rows2, [("A", 0), ("B", 10)])

    def test_rejections(self):
        bad = [
            "住户号,评分\nA,11",          # out of range
            "住户号,评分\nA,x",           # non-integer
            "住户号,评分\nA",             # one column
            "住户号,评分\nA,5\nA,6",     # duplicate
            "住户号,评分\n ,5",           # empty id
            "住户号,评分",                # no records
        ]
        for text in bad:
            with self.assertRaises(CSVValidationError):
                parse_scores(text.encode("utf-8"))

    def test_bom_tolerated(self):
        raw = b"\xef\xbb\xbfhousehold,score\nH1,7\nH2,8\n"
        self.assertEqual(parse_scores(raw), [("H1", 7), ("H2", 8)])


class StorageAndAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.conn = connect(self.db_path)
        init_db(self.conn)
        self.rows = [(f"H{i:03d}", (i * 3) % 11) for i in range(30)]
        replace_original(self.conn, self.rows, "2026-11-23T00:00:00+00:00")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_original_table_kept_as_is(self):
        stored = self.conn.execute(
            "SELECT household_id, score FROM original_scores ORDER BY household_id"
        ).fetchall()
        self.assertEqual([(r["household_id"], r["score"]) for r in stored],
                         sorted(self.rows))
        # mutate corrected table; original must stay untouched
        apply_correction(self.conn, "H000", "update", 10)
        apply_correction(self.conn, "H001", "remove", None)
        stored2 = self.conn.execute(
            "SELECT household_id, score FROM original_scores ORDER BY household_id"
        ).fetchall()
        self.assertEqual([(r["household_id"], r["score"]) for r in stored2],
                         sorted(self.rows))

    def test_summaries_match_household_level_computation(self):
        ids = [h for h, _ in self.rows]
        weights = {h: household_weights(h) for h in ids}
        for table, score_col in (("orig_rounds", None),):
            pass
        rows_db = {r["round_index"]: (r["weight_total"], r["weighted_sum"])
                   for r in self.conn.execute(
                       "SELECT round_index, weight_total, weighted_sum FROM orig_rounds")}
        score_map = dict(self.rows)
        for rnd in range(1000):
            exp_w = sum(weights[h][rnd] for h in ids)
            exp_ws = sum(weights[h][rnd] * score_map[h] for h in ids)
            self.assertEqual(rows_db[rnd], (exp_w, exp_ws))

    def test_shared_weights_then_correction_only_changes_that_household(self):
        before = {r["round_index"]: (r["weight_total"], r["weighted_sum"])
                  for r in self.conn.execute(
                      "SELECT round_index, weight_total, weighted_sum FROM corr_rounds")}
        # identical summaries at start (same tables, shared weights)
        orig = {r["round_index"]: (r["weight_total"], r["weighted_sum"])
                for r in self.conn.execute(
                    "SELECT round_index, weight_total, weighted_sum FROM orig_rounds")}
        self.assertEqual(before, orig)

        apply_correction(self.conn, "H005", "update", 10)
        old_score = dict(self.rows)["H005"]
        after = {r["round_index"]: (r["weight_total"], r["weighted_sum"])
                 for r in self.conn.execute(
                     "SELECT round_index, weight_total, weighted_sum FROM corr_rounds")}
        w5 = household_weights("H005")
        for rnd in range(1000):
            # weight totals unchanged by a score edit; weighted sum shifts
            self.assertEqual(after[rnd][0], before[rnd][0])
            self.assertEqual(after[rnd][1],
                             before[rnd][1] + w5[rnd] * (10 - old_score))
        # original summary never changes
        orig2 = {r["round_index"]: (r["weight_total"], r["weighted_sum"])
                 for r in self.conn.execute(
                     "SELECT round_index, weight_total, weighted_sum FROM orig_rounds")}
        self.assertEqual(orig, orig2)

    def test_add_and_remove_household(self):
        apply_correction(self.conn, "NEW1", "add", 9)
        self.assertEqual(get_household(self.conn, "NEW1"),
                         {"household_id": "NEW1",
                          "original_score": None, "corrected_score": 9})
        n = int(self.conn.execute(
            f"SELECT value FROM meta WHERE key='{META_CORR_N}'").fetchone()["value"])
        self.assertEqual(n, 31)

        apply_correction(self.conn, "NEW1", "remove", None)
        self.assertIsNone(get_household(self.conn, "NEW1"))
        n = int(self.conn.execute(
            f"SELECT value FROM meta WHERE key='{META_CORR_N}'").fetchone()["value"])
        self.assertEqual(n, 30)
        # back to identical summaries
        diffs = self.conn.execute(
            "SELECT COUNT(*) AS k FROM corr_rounds c JOIN orig_rounds o "
            "ON o.round_index = c.round_index "
            "WHERE c.weight_total <> o.weight_total OR c.weighted_sum <> o.weighted_sum"
        ).fetchone()["k"]
        self.assertEqual(diffs, 0)

    def test_failed_correction_rolls_back_keeping_previous_result(self):
        apply_correction(self.conn, "H000", "update", 4)
        snap = {r["round_index"]: (r["weight_total"], r["weighted_sum"])
                for r in self.conn.execute(
                    "SELECT round_index, weight_total, weighted_sum FROM corr_rounds")}
        with self.assertRaises(ValueError):
            apply_correction(self.conn, "H000", "add", 5)       # already exists
        with self.assertRaises(ValueError):
            apply_correction(self.conn, "GHOST", "update", 5)   # missing
        with self.assertRaises(ValueError):
            apply_correction(self.conn, "H001", "update", 99)   # score out of range
        after = {r["round_index"]: (r["weight_total"], r["weighted_sum"])
                 for r in self.conn.execute(
                     "SELECT round_index, weight_total, weighted_sum FROM corr_rounds")}
        self.assertEqual(snap, after)
        score = self.conn.execute(
            "SELECT score FROM corrected_scores WHERE household_id='H000'"
        ).fetchone()["score"]
        self.assertEqual(score, 4)

    def test_reset_restores_original(self):
        apply_correction(self.conn, "H000", "update", 10)
        apply_correction(self.conn, "H001", "remove", None)
        reset_corrected(self.conn)
        c = self.conn.execute(
            "SELECT COUNT(*) AS k FROM corrected_scores"
        ).fetchone()["k"]
        o = self.conn.execute(
            "SELECT COUNT(*) AS k FROM original_scores"
        ).fetchone()["k"]
        self.assertEqual(c, o)
        diffs = self.conn.execute(
            "SELECT COUNT(*) AS k FROM corr_rounds c JOIN orig_rounds o "
            "ON o.round_index = c.round_index "
            "WHERE c.weight_total <> o.weight_total OR c.weighted_sum <> o.weighted_sum"
        ).fetchone()["k"]
        self.assertEqual(diffs, 0)

    def test_analysis_values_and_undefined_states(self):
        res = analyze(self.conn)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["valid_rounds"], 1000)
        self.assertEqual(res["unweighted_diff"], 0.0)
        self.assertEqual(res["p025"], 0.0)
        self.assertEqual(res["p975"], 0.0)

        apply_correction(self.conn, "H000", "update", 10)
        res2 = analyze(self.conn)
        self.assertGreater(res2["unweighted_diff"], 0.0)
        self.assertEqual(res2["valid_rounds"], 1000)
        self.assertLessEqual(res2["p025"], res2["p975"])

        # remove everyone -> undefined with reason
        for h, _ in self.rows:
            apply_correction(self.conn, h, "remove", None)
        res3 = analyze(self.conn)
        self.assertEqual(res3["status"], "undefined")
        self.assertIn("无任何住户", res3["reason"])

    def test_tiny_dataset_zero_weight_rounds_are_skipped(self):
        # one household: in every round its Pois(1) weight is 0 with
        # probability e^-1; such rounds must be skipped, not crash
        tmp = tempfile.TemporaryDirectory()
        conn = connect(os.path.join(tmp.name, "tiny.db"))
        init_db(conn)
        replace_original(conn, [("ONLY", 7)], "t")
        res = analyze(conn)
        weights = household_weights("ONLY")
        expected_valid = sum(1 for w in weights if w > 0)
        self.assertEqual(res["valid_rounds"], expected_valid)
        self.assertEqual(res["skipped_rounds"], 1000 - expected_valid)
        # weighted mean of a single household equals its score -> diff 0
        self.assertEqual(res["p025"], 0.0)
        self.assertEqual(res["p975"], 0.0)
        conn.close()
        tmp.cleanup()


class FlaskFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "web.db")
        self.app = create_app(db_path=self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _upload(self, rows, header=True, name="survey.csv"):
        data = {
            "csv_file": (io.BytesIO(csv_bytes(rows, header=header)), name),
        }
        return self.client.post(
            "/upload", data=data, content_type="multipart/form-data",
            follow_redirects=True,
        )

    def test_full_key_operations(self):
        # no data yet
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("尚未上传原表", html)

        rows = [(f"H{i:02d}", (i * 7) % 11) for i in range(20)]
        resp = self._upload(rows)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("上传成功", resp.get_data(as_text=True))
        self.assertIn("有效轮数", resp.get_data(as_text=True))
        self.assertIn("1000 / 1000", resp.get_data(as_text=True))

        # invalid upload is rejected and does not destroy data
        bad = self.client.post(
            "/upload",
            data={"csv_file": (io.BytesIO(b"id,score\nX,99"), "bad.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        self.assertIn("上传被拒绝", bad.get_data(as_text=True))
        still = self.client.get("/").get_data(as_text=True)
        self.assertIn("1000 / 1000", still)

        # score change
        r = self.client.post("/correct", data={
            "household_id": "H00", "action": "update", "score": "10",
        }, follow_redirects=True)
        self.assertIn("已对住户 H00 执行「改分」", r.get_data(as_text=True))

        # add
        r = self.client.post("/correct", data={
            "household_id": "H99", "action": "add", "score": "2",
        }, follow_redirects=True)
        self.assertIn("补录", r.get_data(as_text=True))

        # single-household lookup reads only that household
        r = self.client.get("/?household_id=H99")
        body = r.get_data(as_text=True)
        self.assertIn("H99", body)
        self.assertIn("原表无此户", body)

        # invalid correction: error flash, previous result kept
        r = self.client.post("/correct", data={
            "household_id": "H01", "action": "update", "score": "50",
        }, follow_redirects=True)
        self.assertIn("更正失败", r.get_data(as_text=True))

        # remove
        r = self.client.post("/correct", data={
            "household_id": "H99", "action": "remove",
        }, follow_redirects=True)
        self.assertIn("移除", r.get_data(as_text=True))

        # reset
        r = self.client.post("/reset", follow_redirects=True)
        self.assertIn("重置", r.get_data(as_text=True))
        body = r.get_data(as_text=True)
        self.assertIn("+0.0000", body)  # unweighted diff back to zero

        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_all_removed_shows_reason_in_page(self):
        self._upload([("A", 3), ("B", 5)])
        for h in ("A", "B"):
            self.client.post("/correct", data={
                "household_id": h, "action": "remove",
            }, follow_redirects=True)
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("无法计算", body)
        self.assertIn("更正表已无任何住户", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class IncrementalUploadTests(unittest.TestCase):
    """Re-upload only generates weights for added/removed/score-changed rows."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "inc.db")
        self.conn = connect(self.db_path)
        init_db(self.conn)
        self.rows = [(f"H{i:03d}", (i * 5) % 11) for i in range(60)]
        self.first = replace_original(self.conn, self.rows, "t0")
        self.assertEqual(self.first["recomputed_count"], 60)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _summaries(self):
        return {
            (r["round_index"]): (r["weight_total"], r["weighted_sum"])
            for r in self.conn.execute(
                "SELECT round_index, weight_total, weighted_sum FROM orig_rounds"
            )
        }

    def _reupload(self, new_rows, base=None):
        if base is None:
            base = load_upload_baseline(self.conn)
        plan = build_upload_plan(new_rows, base["rows"])
        outcome = reupload_original(
            self.conn, new_rows, "t1",
            base["fingerprint"], base["generation"], plan,
        )
        return plan, outcome

    def test_only_changed_households_recomputed_and_sums_match_full(self):
        new_rows = [tuple(r) for r in self.rows]
        new_rows[3] = (new_rows[3][0], (new_rows[3][1] + 1) % 11)  # score change
        new_rows[10] = (new_rows[10][0], new_rows[10][1])           # unchanged
        new_rows.append(("NEW-1", 8))                                # added
        new_rows = [r for r in new_rows if r[0] != "H020"]           # removed
        plan, outcome = self._reupload(new_rows)
        self.assertEqual(plan["changed_count"], 3)
        self.assertEqual(outcome["recomputed_count"], 3)
        self.assertEqual(outcome["mode"], "incremental")
        self.assertEqual(analyze(self.conn)["orig_n"], len(new_rows))
        # corrected table re-initialised from the new original
        self.assertEqual(
            [(r["household_id"], r["score"]) for r in self.conn.execute(
                "SELECT household_id, score FROM corrected_scores ORDER BY household_id")],
            sorted(new_rows),
        )
        incremental = self._summaries()
        corr = {r["round_index"]: (r["weight_total"], r["weighted_sum"])
                for r in self.conn.execute(
                    "SELECT round_index, weight_total, weighted_sum FROM corr_rounds")}
        self.assertEqual(incremental, corr)
        # full recomputation in a fresh database must match bit for bit
        tmp2 = tempfile.TemporaryDirectory()
        conn2 = connect(os.path.join(tmp2.name, "full.db"))
        init_db(conn2)
        replace_original(conn2, new_rows, "t1")
        full = {(r["round_index"]): (r["weight_total"], r["weighted_sum"])
                for r in conn2.execute(
                    "SELECT round_index, weight_total, weighted_sum FROM orig_rounds")}
        self.assertEqual(incremental, full)
        conn2.close()
        tmp2.cleanup()

    def test_reorder_only_generates_zero_household_weights(self):
        before = self._summaries()
        reordered = list(reversed(self.rows))
        import app.db as db_mod
        real = db_mod.household_weights
        calls = []

        def counting(hid, *a, **kw):
            calls.append(hid)
            return real(hid, *a, **kw)

        db_mod.household_weights = counting
        try:
            plan, outcome = self._reupload(reordered)
        finally:
            db_mod.household_weights = real
        self.assertEqual(plan["changed_count"], 0)
        self.assertEqual(outcome["recomputed_count"], 0)
        self.assertEqual(calls, [])
        self.assertEqual(self._summaries(), before)

    def test_conflict_on_stale_baseline_even_with_identical_content(self):
        stale = load_upload_baseline(self.conn)
        # another upload (any content, here identical) commits first
        self._reupload([tuple(r) for r in self.rows])
        snapshot = self._summaries()
        plan = build_upload_plan([tuple(r) for r in self.rows], stale["rows"])
        with self.assertRaises(UploadConflict):
            reupload_original(
                self.conn, [tuple(r) for r in self.rows], "t2",
                stale["fingerprint"], stale["generation"], plan,
            )
        self.assertEqual(self._summaries(), snapshot)
        self.assertEqual(analyze(self.conn)["orig_n"], len(self.rows))

    def test_corrections_during_planning_commit_and_upload_still_applies(self):
        base = load_upload_baseline(self.conn)
        new_rows = [tuple(r) for r in self.rows]
        new_rows[0] = (new_rows[0][0], 10)
        plan = build_upload_plan(new_rows, base["rows"])  # long-running step
        # a per-household correction lands while the plan is computed
        apply_correction(self.conn, "H005", "update", 3)
        self.assertEqual(get_household(self.conn, "H005")["corrected_score"], 3)
        outcome = reupload_original(
            self.conn, new_rows, "t1",
            base["fingerprint"], base["generation"], plan,
        )
        self.assertEqual(outcome["recomputed_count"], 1)
        # upload succeeded and the corrected table starts from the new original
        self.assertEqual(analyze(self.conn)["corr_n"], len(new_rows))
        # the in-flight correction is superseded: corrected copy == new original
        self.assertEqual(
            get_household(self.conn, "H005")["corrected_score"],
            dict(new_rows)["H005"],
        )
        self.assertEqual(analyze(self.conn)["orig_n"], len(new_rows))

    def test_failed_write_keeps_previous_data(self):
        base = load_upload_baseline(self.conn)
        new_rows = [("X", 1)]
        plan = build_upload_plan(new_rows, base["rows"])
        before = self._summaries()
        # corrupt the summary count so the guarded write refuses to run
        self.conn.execute("DELETE FROM orig_rounds WHERE round_index = 0")
        self.conn.commit()
        with self.assertRaises(RuntimeError):
            reupload_original(
                self.conn, new_rows, "t1",
                base["fingerprint"], base["generation"], plan,
            )
        self.assertEqual(analyze(self.conn)["orig_n"], len(self.rows))
        stored = [r["household_id"] for r in self.conn.execute(
            "SELECT household_id FROM original_scores")]
        self.assertEqual(len(stored), len(self.rows))

    def test_legacy_database_is_upgraded_in_place(self):
        # simulate a pre-incremental database: drop the new bookkeeping keys
        for key in ("orig_fingerprint", "upload_generation", "last_recomputed_count",
                    "last_upload_mode", "last_upload_at"):
            self.conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        self.conn.commit()
        status = get_upload_status(self.conn)
        self.assertIsNotNone(status["generation"])
        self.assertEqual(status["orig_n"], len(self.rows))
        new_rows = [tuple(r) for r in self.rows]
        new_rows[0] = (new_rows[0][0], (new_rows[0][1] + 1) % 11)
        plan, outcome = self._reupload(new_rows, load_upload_baseline(self.conn))
        self.assertEqual(outcome["recomputed_count"], 1)
        self.assertEqual(analyze(self.conn)["orig_n"], len(self.rows))


class IncrementalFlaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "web-inc.db")
        self.app = create_app(db_path=self.db_path)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def _upload(self, rows, base=None):
        data = {
            "csv_file": (io.BytesIO(csv_bytes(rows, header=True)), "s.csv"),
        }
        if base is not None:
            data["base_generation"] = str(base["generation"])
            data["base_fingerprint"] = base["fingerprint"]
        return self.client.post("/upload", data=data,
                                content_type="multipart/form-data",
                                follow_redirects=True)

    def _page_baseline(self):
        import re
        body = self.client.get("/").get_data(as_text=True)
        gen = re.search(r'name="base_generation" value="([^"]*)"', body)
        fp = re.search(r'name="base_fingerprint" value="([^"]*)"', body)
        return {"generation": int(gen.group(1) or 0), "fingerprint": fp.group(1)}

    def test_web_shows_recomputed_count_and_incremental_flow(self):
        rows = [(f"H{i:02d}", i % 11) for i in range(25)]
        first = self._upload(rows)
        body = first.get_data(as_text=True)
        self.assertIn("首次上传成功", body)
        self.assertIn("本次生成权重的户数", body)
        self.assertIn("25 户", body)
        self.assertIn("全量", body)

        base = self._page_baseline()
        changed = [tuple(r) for r in rows]
        changed[2] = (changed[2][0], (changed[2][1] + 2) % 11)
        changed.append(("H99", 7))
        changed = [r for r in changed if r[0] != "H00"]
        resp = self._upload(changed, base=base)
        body = resp.get_data(as_text=True)
        self.assertIn("上传成功", body)
        self.assertIn("本次为 3 户生成抽样权重", body)
        self.assertIn("增量（仅变更户）", body)
        self.assertIn("3 户", body)

    def test_stale_page_upload_is_returned_and_data_unchanged(self):
        rows = [(f"H{i:02d}", i % 11) for i in range(10)]
        self._upload(rows)
        stale = self._page_baseline()
        other = [tuple(r) for r in rows]
        other[0] = (other[0][0], 9)
        self._upload(other)  # another tab commits first
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("+", page)  # dataset still present
        resp = self._upload([tuple(r) for r in rows], base=stale)
        body = resp.get_data(as_text=True)
        self.assertIn("本次上传已退回", body)
        self.assertIn("请重新上传", body)
        # current dataset is the other tab's, not the stale one
        conn = connect(self.db_path)
        try:
            self.assertEqual(
                conn.execute("SELECT score FROM original_scores WHERE household_id='H00'")
                .fetchone()["score"], 9)
        finally:
            conn.close()
        # retry with the freshly rendered baseline succeeds
        retried = self._upload([tuple(r) for r in rows],
                               base=self._page_baseline())
        self.assertIn("上传成功", retried.get_data(as_text=True))

    def test_reorder_upload_reports_zero_households(self):
        rows = [(f"H{i:02d}", i % 11) for i in range(15)]
        self._upload(rows)
        resp = self._upload(list(reversed(rows)), base=self._page_baseline())
        body = resp.get_data(as_text=True)
        self.assertIn("本次为 0 户生成抽样权重", body)

    def test_concurrent_correction_then_upload_applies(self):
        rows = [(f"H{i:02d}", i % 11) for i in range(12)]
        self._upload(rows)
        base = self._page_baseline()
        # a correction commits between page render and the upload
        self.client.post("/correct", data={
            "household_id": "H03", "action": "update", "score": "10",
        }, follow_redirects=True)
        changed = [tuple(r) for r in rows]
        changed[0] = (changed[0][0], (changed[0][1] + 1) % 11)
        resp = self._upload(changed, base=base)
        body = resp.get_data(as_text=True)
        self.assertIn("上传成功", body)
        self.assertIn("本次为 1 户生成抽样权重", body)
        # corrected table re-initialised from the new original (correction gone)
        conn = connect(self.db_path)
        try:
            c = conn.execute(
                "SELECT score FROM corrected_scores WHERE household_id='H03'"
            ).fetchone()["score"]
            self.assertEqual(c, changed[3][1])
        finally:
            conn.close()

    def test_two_first_uploads_from_blank_page_only_one_wins(self):
        rows = [(f"H{i:02d}", i % 11) for i in range(8)]
        # both submissions were rendered while the database was still empty
        blank = {"generation": 0, "fingerprint": ""}
        first = self._upload(rows, base=blank)
        self.assertIn("首次上传成功", first.get_data(as_text=True))
        second = self._upload([("X", 5)], base=blank)
        body = second.get_data(as_text=True)
        self.assertIn("本次上传已退回", body)
        conn = connect(self.db_path)
        try:
            ids = [r["household_id"] for r in conn.execute(
                "SELECT household_id FROM original_scores ORDER BY household_id")]
            self.assertEqual(ids, [h for h, _ in rows])
        finally:
            conn.close()

    def test_validation_failure_leaves_current_data(self):
        rows = [(f"H{i:02d}", i % 11) for i in range(8)]
        self._upload(rows)
        base = self._page_baseline()
        resp = self.client.post("/upload", data={
            "base_generation": str(base["generation"]),
            "base_fingerprint": base["fingerprint"],
            "csv_file": (io.BytesIO(b"id,score\nH00,99"), "bad.csv"),
        }, content_type="multipart/form-data", follow_redirects=True)
        body = resp.get_data(as_text=True)
        self.assertIn("上传被拒绝", body)
        self.assertIn("8 户", body)

    def test_force_full_matches_incremental_and_is_marked_full(self):
        rows = [(f"H{i:02d}", i % 11) for i in range(30)]
        self._upload(rows)
        base = self._page_baseline()
        changed = [tuple(r) for r in rows]
        changed[0] = (changed[0][0], 10)
        # incremental first
        inc = self._upload(changed, base=base)
        inc_summary = self._summary_snapshot()
        self.assertIn("本次为 1 户生成抽样权重", inc.get_data(as_text=True))
        self.assertIn("增量（仅变更户）", inc.get_data(as_text=True))
        # then force a full recompute of the same content
        base2 = self._page_baseline()
        data = {
            "csv_file": (io.BytesIO(csv_bytes(changed)), "s.csv"),
            "base_generation": str(base2["generation"]),
            "base_fingerprint": base2["fingerprint"],
            "force_full": "1",
        }
        resp = self.client.post("/upload", data=data,
                                content_type="multipart/form-data",
                                follow_redirects=True)
        body = resp.get_data(as_text=True)
        self.assertIn("全量重算成功", body)
        self.assertIn("本次为全部 30 户生成抽样权重", body)
        self.assertEqual(self._summary_snapshot(), inc_summary)

    def test_chained_incremental_equals_full_recompute(self):
        import app.db as m
        rows = [(f"K{i:03d}", i % 11) for i in range(50)]
        self._upload(rows)
        for step in range(3):
            base = self._page_baseline()
            rows = [tuple(r) for r in rows]
            rows[step * 5] = (rows[step * 5][0], (rows[step * 5][1] + 4) % 11)
            rows.append((f"NEW{step}", step + 1))
            rows = [r for r in rows if r[0] != f"K{step:03d}"]
            self._upload(rows, base=base)
        inc = self._summary_snapshot()
        tmp = tempfile.TemporaryDirectory()
        conn = connect(os.path.join(tmp.name, "f.db"))
        init_db(conn)
        replace_original(conn, rows, "t")
        full = {(r["round_index"]): (r["weight_total"], r["weighted_sum"])
                for r in conn.execute(
                    "SELECT round_index, weight_total, weighted_sum FROM orig_rounds")}
        conn.close()
        tmp.cleanup()
        self.assertEqual(inc, full)

    def _summary_snapshot(self):
        conn = connect(self.db_path)
        try:
            return {(r["round_index"]): (r["weight_total"], r["weighted_sum"])
                    for r in conn.execute(
                        "SELECT round_index, weight_total, weighted_sum FROM orig_rounds")}
        finally:
            conn.close()

    def test_exception_during_write_rolls_back_and_keeps_current_data(self):
        import app.db as m
        rows = [(f"H{i:02d}", i % 11) for i in range(12)]
        self._upload(rows)
        snapshot = self._summary_snapshot()
        conn_snapshot = connect(self.db_path)
        try:
            orig_before = sorted((r["household_id"], r["score"]) for r in conn_snapshot.execute(
                "SELECT household_id, score FROM original_scores"))
        finally:
            conn_snapshot.close()
        base = load_upload_baseline(connect(self.db_path))
        conn = connect(self.db_path)
        try:
            new_rows = [("Z", 4)]
            plan = build_upload_plan(new_rows, base["rows"])
            real = m._write_dataset
            def boom(*a, **kw):
                real(*a, **kw)      # write, then fail before COMMIT
                raise RuntimeError("simulated disk error")
            m._write_dataset = boom
            try:
                with self.assertRaises(RuntimeError):
                    reupload_original(conn, new_rows, "t",
                                      base["fingerprint"], base["generation"], plan)
            finally:
                m._write_dataset = real
            self.assertFalse(conn.in_transaction)
            self.assertEqual(
                sorted((r["household_id"], r["score"]) for r in conn.execute(
                    "SELECT household_id, score FROM original_scores")),
                orig_before)
        finally:
            conn.close()
        self.assertEqual(self._summary_snapshot(), snapshot)

    def test_inconsistent_summary_is_refused_until_forced_full(self):
        rows = [(f"H{i:02d}", i % 11) for i in range(10)]
        self._upload(rows)
        base = self._page_baseline()
        conn = connect(self.db_path)
        try:
            conn.execute(
                "UPDATE orig_rounds SET weighted_sum = weighted_sum + 7 "
                "WHERE round_index = 5")
            conn.commit()
        finally:
            conn.close()
        changed = [tuple(r) for r in rows]
        changed[0] = (changed[0][0], 10)
        resp = self._upload(changed, base=base)
        body = resp.get_data(as_text=True)
        self.assertIn("入库失败", body)
        self.assertIn("强制全量重算", body)
        # force full repairs and reproduces the correct summary
        base2 = self._page_baseline()
        data = {
            "csv_file": (io.BytesIO(csv_bytes(changed)), "s.csv"),
            "base_generation": str(base2["generation"]),
            "base_fingerprint": base2["fingerprint"],
            "force_full": "1",
        }
        resp2 = self.client.post("/upload", data=data,
                                 content_type="multipart/form-data",
                                 follow_redirects=True)
        self.assertIn("全量重算成功", resp2.get_data(as_text=True))
        tmp = tempfile.TemporaryDirectory()
        conn = connect(os.path.join(tmp.name, "ref.db"))
        init_db(conn)
        replace_original(conn, changed, "t")
        expected = {(r["round_index"]): (r["weight_total"], r["weighted_sum"])
                    for r in conn.execute(
                        "SELECT round_index, weight_total, weighted_sum FROM orig_rounds")}
        conn.close()
        tmp.cleanup()
        self.assertEqual(self._summary_snapshot(), expected)

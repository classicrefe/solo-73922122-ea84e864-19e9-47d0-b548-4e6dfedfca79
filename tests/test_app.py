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
    apply_correction,
    connect,
    get_household,
    init_db,
    replace_original,
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

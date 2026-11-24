"""Flask review application: upload, per-household corrections, analysis view."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from flask import Flask, flash, redirect, render_template, request, url_for

from .csv_io import CSVValidationError, parse_scores
from .db import (
    UploadConflict,
    apply_correction,
    build_upload_plan,
    connect,
    empty_fingerprint,
    enable_wal,
    get_household,
    get_upload_status,
    has_dataset,
    init_db,
    load_upload_baseline,
    reupload_original,
    reset_corrected,
)
from .stats import analyze


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")
    app.config["DB_PATH"] = db_path or os.environ.get(
        "DB_PATH", os.path.join(os.getcwd(), "data", "survey.db")
    )
    os.makedirs(os.path.dirname(app.config["DB_PATH"]), exist_ok=True)

    def get_conn():
        conn = connect(app.config["DB_PATH"])
        init_db(conn)
        return conn

    # One-time upgrade to WAL so reads don't block an upload's write
    # transaction.  Done at startup (outside request transactions).
    startup_conn = connect(app.config["DB_PATH"])
    try:
        init_db(startup_conn)
        enable_wal(startup_conn)
    finally:
        startup_conn.close()

    @app.route("/", methods=["GET"])
    def index():
        conn = get_conn()
        try:
            uploaded = has_dataset(conn)
            upload_status = get_upload_status(conn) if uploaded else None
            result = analyze(conn) if uploaded else None
            lookup = None
            lookup_id = request.args.get("household_id", "").strip()
            if uploaded and lookup_id:
                lookup = get_household(conn, lookup_id)
                if lookup is None:
                    flash(f"未找到住户 {lookup_id}（原表与更正表均无此户）。", "warn")
            return render_template(
                "index.html", result=result, uploaded=uploaded,
                upload_status=upload_status,
                lookup=lookup, lookup_id=lookup_id,
            )
        finally:
            conn.close()

    @app.route("/upload", methods=["POST"])
    def upload():
        file = request.files.get("csv_file")
        if file is None or file.filename == "":
            flash("请选择要上传的 CSV 文件。", "error")
            return redirect(url_for("index"))
        raw = file.read()
        try:
            rows = parse_scores(raw)
        except CSVValidationError as exc:
            flash(f"上传被拒绝，数据库未改动：{exc}", "error")
            return redirect(url_for("index"))

        # Baseline the page was showing when the file was chosen.  A blank
        # database is submitted as generation 0, so two tabs opened on an empty
        # page still conflict once one of them has committed a first upload.
        raw_generation = request.form.get("base_generation", "").strip()
        raw_fingerprint = request.form.get("base_fingerprint", "").strip()
        # generation 0 means "page rendered against an empty database" and
        # carries no fingerprint; every other baseline needs both values.
        if raw_generation != "0" and bool(raw_generation) != bool(raw_fingerprint):
            flash("上传被拒绝，数据库未改动：基准参数不完整，请刷新页面后重试。", "error")
            return redirect(url_for("index"))
        try:
            base_generation = int(raw_generation) if raw_generation else None
        except ValueError:
            flash("上传被拒绝，数据库未改动：基准参数无效，请刷新页面后重试。", "error")
            return redirect(url_for("index"))
        base_fingerprint = raw_fingerprint or None
        if base_generation == 0:
            base_fingerprint = empty_fingerprint()
        has_baseline = base_generation is not None

        conn = get_conn()
        try:
            # Read the committed original table and compute the plan outside the
            # write transaction; the page stays usable for queries/corrections.
            baseline = load_upload_baseline(conn)
            if baseline is None:
                if has_baseline and base_generation != 0:
                    raise UploadConflict("基准已变化（另一笔上传已先入库）")
                baseline_rows = []
            else:
                if has_baseline and (
                    base_generation == 0
                    or baseline["generation"] != base_generation
                    or baseline["fingerprint"] != base_fingerprint
                ):
                    raise UploadConflict("基准已被另一笔原表上传更新")
                # No baseline supplied (non-page caller): plan against the
                # current committed table.  The fingerprint guard below still
                # aborts if it changes before the write transaction starts.
                baseline_rows = baseline["rows"]

            force_full = request.form.get("force_full") == "1"
            plan = build_upload_plan(rows, baseline_rows, force_full=force_full)
            if has_baseline and base_generation == 0 and baseline is None:
                # first upload from a blank page: still guarded against another
                # upload slipping in before the write transaction starts
                base_generation = None
                base_fingerprint = None
            elif not has_baseline and baseline is not None:
                # non-page caller: bind the plan to the baseline read above
                base_generation = baseline["generation"]
                base_fingerprint = baseline["fingerprint"]
            outcome = reupload_original(
                conn, rows,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                base_fingerprint, base_generation, plan,
            )
        except UploadConflict as exc:
            conn.rollback()
            flash(
                f"本次上传已退回，数据库未改动：{exc}。"
                "另一笔原表上传已先入库，请重新上传本文件。",
                "error",
            )
            return redirect(url_for("index"))
        except ValueError as exc:
            conn.rollback()
            flash(f"上传被拒绝，数据库未改动：{exc}", "error")
            return redirect(url_for("index"))
        except Exception as exc:  # pragma: no cover - defensive rollback path
            conn.rollback()
            flash(f"入库失败，已保留上一结果：{exc}", "error")
            return redirect(url_for("index"))
        finally:
            conn.close()

        if outcome["mode"] == "full" and outcome["first_upload"]:
            flash(
                f"首次上传成功：{outcome['household_count']} 户已原样入库，"
                f"本次为全部 {outcome['recomputed_count']} 户生成抽样权重；"
                "更正表已初始化为原表副本。",
                "ok",
            )
        elif outcome["mode"] == "full":
            flash(
                f"全量重算成功：原表 {outcome['household_count']} 户已更新，"
                f"本次为全部 {outcome['recomputed_count']} 户生成抽样权重；"
                "更正表已从新原表重新初始化，轮次汇总已重建。",
                "ok",
            )
        else:
            flash(
                f"上传成功：原表 {outcome['household_count']} 户已更新，"
                f"本次为 {outcome['recomputed_count']} 户生成抽样权重"
                "（新增、移除或评分有变的住户），其余住户沿用已有权重；"
                "更正表已从新原表重新初始化，轮次汇总已同步生效。",
                "ok",
            )
        return redirect(url_for("index"))

    @app.route("/correct", methods=["POST"])
    def correct():
        household_id = request.form.get("household_id", "").strip()
        action = request.form.get("action", "").strip()
        score_text = request.form.get("score", "").strip()
        if not household_id:
            flash("请填写住户号。", "error")
            return redirect(url_for("index"))

        new_score: int | None = None
        if action != "remove":
            try:
                new_score = int(score_text)
            except ValueError:
                flash("评分必须是 0～10 的整数。", "error")
                return redirect(url_for("index"))

        conn = get_conn()
        try:
            if not has_dataset(conn):
                flash("尚未上传原表，无法更正。", "error")
                return redirect(url_for("index"))
            apply_correction(conn, household_id, action, new_score)
        except ValueError as exc:
            conn.rollback()
            flash(f"更正失败，已保留上一结果：{exc}", "error")
            return redirect(url_for("index"))
        except Exception as exc:  # pragma: no cover - defensive rollback path
            conn.rollback()
            flash(f"更正失败，已保留上一结果：{exc}", "error")
            return redirect(url_for("index"))
        finally:
            conn.close()

        verb = {"add": "补录", "update": "改分", "remove": "移除"}.get(action, action)
        flash(f"已对住户 {household_id} 执行「{verb}」，更正表与汇总已一同生效。", "ok")
        return redirect(url_for("index"))

    @app.route("/reset", methods=["POST"])
    def reset():
        conn = get_conn()
        try:
            if not has_dataset(conn):
                flash("尚未上传原表，无需重置。", "warn")
                return redirect(url_for("index"))
            reset_corrected(conn)
        except Exception as exc:  # pragma: no cover - defensive rollback path
            conn.rollback()
            flash(f"重置失败，已保留上一结果：{exc}", "error")
            return redirect(url_for("index"))
        finally:
            conn.close()
        flash("更正表已重置为原表副本，汇总已同步重建。", "ok")
        return redirect(url_for("index"))

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app

import os
import sys
import json
from datetime import datetime

from flask import Flask, jsonify, request, render_template, send_from_directory

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import optimizer
from ml import model_pipeline
from ml import railways_data

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")

app = Flask(
    __name__,
    template_folder=os.path.join(FRONTEND_DIR, "templates"),
    static_folder=os.path.join(FRONTEND_DIR, "static"),
)


@app.errorhandler(Exception)
def handle_any_error(e):
    """Guarantee every /api/* response is valid JSON, even on an unexpected
    server error, so the frontend never tries to JSON-parse an HTML error
    page (which shows up in the browser as 'Unexpected token')."""
    from werkzeug.exceptions import HTTPException
    if request.path.startswith("/api/"):
        code = e.code if isinstance(e, HTTPException) else 500
        return jsonify({"error": str(e)}), code
    raise e


def row_to_dict(row):
    return dict(row) if row else None


def rows_to_list(rows):
    return [dict(r) for r in rows]


# ----------------------------------------------------------------- page --
@app.route("/")
def index():
    return render_template("index.html")


# ------------------------------------------------------------ dashboard --
@app.route("/api/dashboard")
def dashboard():
    conn = db.get_conn()
    total_assets = conn.execute("SELECT COUNT(*) c FROM assets").fetchone()["c"]
    degraded_assets = conn.execute(
        "SELECT COUNT(*) c FROM assets WHERE status != 'Operational'"
    ).fetchone()["c"]
    pending = conn.execute(
        "SELECT COUNT(*) c FROM maintenance_requests WHERE status = 'Pending'"
    ).fetchone()["c"]
    optimized = conn.execute(
        "SELECT COUNT(*) c FROM maintenance_requests WHERE status = 'Optimized'"
    ).fetchone()["c"]
    approved = conn.execute(
        "SELECT COUNT(*) c FROM maintenance_requests WHERE status = 'Approved'"
    ).fetchone()["c"]
    conflicts = conn.execute(
        "SELECT COUNT(*) c FROM maintenance_requests WHERE conflict IS NOT NULL"
    ).fetchone()["c"]
    avg_condition = conn.execute("SELECT AVG(condition_score) a FROM assets").fetchone()["a"] or 0
    avg_disruption = conn.execute(
        "SELECT AVG(disruption_score) a FROM maintenance_requests WHERE disruption_score IS NOT NULL"
    ).fetchone()["a"]

    by_status = rows_to_list(conn.execute(
        "SELECT status, COUNT(*) as count FROM maintenance_requests GROUP BY status"
    ).fetchall())
    by_line = rows_to_list(conn.execute(
        """SELECT a.line, COUNT(*) as count FROM maintenance_requests mr
           JOIN assets a ON a.id = mr.asset_id GROUP BY a.line"""
    ).fetchall())
    by_asset_type = rows_to_list(conn.execute(
        """SELECT a.asset_type, COUNT(*) as count FROM maintenance_requests mr
           JOIN assets a ON a.id = mr.asset_id GROUP BY a.asset_type"""
    ).fetchall())
    severity_trend = rows_to_list(conn.execute(
        """SELECT reported_date, AVG(defect_severity) as avg_severity, COUNT(*) as count
           FROM maintenance_requests GROUP BY reported_date ORDER BY reported_date"""
    ).fetchall())
    condition_by_line = rows_to_list(conn.execute(
        "SELECT line, AVG(condition_score) as avg_condition FROM assets GROUP BY line"
    ).fetchall())
    conn.close()

    return jsonify({
        "total_assets": total_assets,
        "degraded_assets": degraded_assets,
        "pending_requests": pending,
        "optimized_requests": optimized,
        "approved_requests": approved,
        "conflicts": conflicts,
        "avg_condition_score": round(avg_condition, 1),
        "avg_disruption_score": round(avg_disruption, 1) if avg_disruption is not None else None,
        "by_status": by_status,
        "by_line": by_line,
        "by_asset_type": by_asset_type,
        "severity_trend": severity_trend,
        "condition_by_line": condition_by_line,
    })


# ---------------------------------------------------------------- assets --
@app.route("/api/assets", methods=["GET"])
def list_assets():
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM assets ORDER BY criticality DESC, name").fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


@app.route("/api/assets/<int:asset_id>", methods=["GET"])
def get_asset(asset_id):
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Asset not found"}), 404
    return jsonify(row_to_dict(row))


# ------------------------------------------------------------- resources --
@app.route("/api/resources", methods=["GET"])
def list_resources():
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM resources ORDER BY resource_type, name").fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


# --------------------------------------------------------------- trains --
@app.route("/api/schedules", methods=["GET"])
def list_schedules():
    line = request.args.get("line")
    conn = db.get_conn()
    if line:
        rows = conn.execute(
            "SELECT * FROM train_schedules WHERE line = ? ORDER BY departure", (line,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM train_schedules ORDER BY line, departure").fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


# ------------------------------------------------------- safety constraints --
@app.route("/api/constraints", methods=["GET"])
def list_constraints():
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM safety_constraints").fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


# --------------------------------------------------------------- requests --
@app.route("/api/requests", methods=["GET"])
def list_requests():
    status = request.args.get("status")
    conn = db.get_conn()
    q = """SELECT mr.*, a.name as asset_name, a.asset_type, a.line, a.criticality,
                  r.name as resource_name
           FROM maintenance_requests mr
           JOIN assets a ON a.id = mr.asset_id
           LEFT JOIN resources r ON r.id = mr.recommended_resource_id"""
    params = []
    if status:
        q += " WHERE mr.status = ?"
        params.append(status)
    q += " ORDER BY COALESCE(mr.priority_score, 0) DESC, mr.reported_date"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("priority_breakdown"):
            try:
                d["priority_breakdown"] = json.loads(d["priority_breakdown"])
            except Exception:
                pass
        out.append(d)
    return jsonify(out)


@app.route("/api/requests", methods=["POST"])
def create_request():
    data = request.get_json(force=True)
    required = ["asset_id", "title", "defect_severity", "duration_minutes"]
    missing = [f for f in required if f not in data or data[f] in (None, "")]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    conn = db.get_conn()
    asset = conn.execute("SELECT id FROM assets WHERE id = ?", (data["asset_id"],)).fetchone()
    if not asset:
        conn.close()
        return jsonify({"error": "Unknown asset_id"}), 400

    cur = conn.execute(
        """INSERT INTO maintenance_requests
           (asset_id, title, description, defect_severity, duration_minutes, reported_date, status)
           VALUES (?,?,?,?,?,?, 'Pending')""",
        (
            data["asset_id"], data["title"], data.get("description", ""),
            int(data["defect_severity"]), int(data["duration_minutes"]),
            data.get("reported_date", datetime.now().date().isoformat()),
        ),
    )
    db.log_activity(conn, data.get("actor", "operator"), "create_request",
                     f"Created request #{cur.lastrowid}: {data['title']}")
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return jsonify({"id": new_id, "status": "Pending"}), 201


@app.route("/api/requests/<int:req_id>/approve", methods=["POST"])
def approve_request(req_id):
    data = request.get_json(silent=True) or {}
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM maintenance_requests WHERE id = ?", (req_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Request not found"}), 404
    # If not yet optimized, run the optimizer for pending/optimized rows so this
    # request gets a recommended window, then approve it.
    if not row["recommended_date"]:
        try:
            optimizer.optimize(conn, only_status=("Pending", "Optimized"))
            conn.commit()
            row = conn.execute("SELECT * FROM maintenance_requests WHERE id = ?", (req_id,)).fetchone()
        except Exception as e:
            conn.close()
            return jsonify({"error": f"Could not optimize before approve: {e}"}), 500
    if not row or not row["recommended_date"]:
        conn.close()
        return jsonify({
            "error": "No available possession window found for this request. "
                     "Try Re-optimize or adjust duration/severity."
        }), 400
    approver = data.get("approved_by", "Duty Manager")
    conn.execute(
        "UPDATE maintenance_requests SET status='Approved', approved_by=? WHERE id=?",
        (approver, req_id),
    )
    db.log_activity(conn, approver, "approve_request", f"Approved request #{req_id}")
    conn.commit()
    conn.close()
    return jsonify({"id": req_id, "status": "Approved"})


@app.route("/api/requests/<int:req_id>/reject", methods=["POST"])
def reject_request(req_id):
    data = request.get_json(silent=True) or {}
    conn = db.get_conn()
    row = conn.execute("SELECT id FROM maintenance_requests WHERE id = ?", (req_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Request not found"}), 404
    conn.execute("UPDATE maintenance_requests SET status='Rejected' WHERE id=?", (req_id,))
    db.log_activity(conn, data.get("actor", "operator"), "reject_request", f"Rejected request #{req_id}")
    conn.commit()
    conn.close()
    return jsonify({"id": req_id, "status": "Rejected"})


@app.route("/api/requests/<int:req_id>/complete", methods=["POST"])
def complete_request(req_id):
    conn = db.get_conn()
    row = conn.execute("SELECT asset_id FROM maintenance_requests WHERE id = ?", (req_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Request not found"}), 404
    conn.execute("UPDATE maintenance_requests SET status='Completed' WHERE id=?", (req_id,))
    conn.execute(
        "UPDATE assets SET last_maintenance=?, condition_score=MIN(100, condition_score + 15) WHERE id=?",
        (datetime.now().date().isoformat(), row["asset_id"]),
    )
    db.log_activity(conn, "field crew", "complete_request", f"Marked request #{req_id} complete")
    conn.commit()
    conn.close()
    return jsonify({"id": req_id, "status": "Completed"})


# -------------------------------------------------------------- optimize --
@app.route("/api/optimize", methods=["POST"])
def run_optimize():
    conn = db.get_conn()
    results = optimizer.optimize(conn, only_status=("Pending", "Optimized"))
    optimizer.apply_results(conn, results)
    db.log_activity(conn, "optimizer", "optimize",
                     f"Optimized {len(results)} request(s); "
                     f"{sum(1 for r in results if r['conflict'])} conflict(s) flagged.")
    conn.commit()
    conn.close()
    return jsonify({"optimized": len(results), "results": results})


@app.route("/api/reoptimize", methods=["POST"])
def run_reoptimize():
    """Re-run optimization for everything not yet approved/completed — e.g. after
    a new urgent request comes in or conditions change."""
    conn = db.get_conn()
    conn.execute(
        """UPDATE maintenance_requests SET status='Pending', recommended_date=NULL,
           recommended_start=NULL, recommended_end=NULL, recommended_resource_id=NULL,
           disruption_score=NULL, conflict=NULL
           WHERE status = 'Optimized'"""
    )
    results = optimizer.optimize(conn, only_status=("Pending",))
    optimizer.apply_results(conn, results)
    db.log_activity(conn, "optimizer", "reoptimize", f"Re-optimized {len(results)} request(s).")
    conn.commit()
    conn.close()
    return jsonify({"optimized": len(results), "results": results})


# ------------------------------------------------------------- ML model --
@app.route("/api/model/status", methods=["GET"])
def model_status():
    return jsonify(model_pipeline.get_status())


@app.route("/api/model/train", methods=["POST"])
def model_train():
    try:
        metrics = model_pipeline.train()
        return jsonify(metrics)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/inspect", methods=["POST"])
def inspect_image():
    asset_id = request.form.get("asset_id", type=int)
    if "image" in request.files and request.files["image"].filename:
        image_bytes = request.files["image"].read()
        try:
            result = model_pipeline.predict_from_image_bytes(image_bytes)
        except Exception as e:
            return jsonify({"error": str(e)}), 400
    else:
        try:
            result = model_pipeline.predict_from_random_demo_sample()
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    if asset_id:
        conn = db.get_conn()
        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if asset:
            conn.execute(
                "UPDATE assets SET ml_defect_probability=?, last_inspection=? WHERE id=?",
                (result["defect_probability"], datetime.now().isoformat(timespec="seconds"), asset_id),
            )
            db.log_activity(conn, "ai-inspection", "inspect_image",
                             f"Asset #{asset_id}: {result['predicted_label']} "
                             f"({result['defect_probability']*100:.1f}% defect probability)")
            conn.commit()
        conn.close()
        result["asset_id"] = asset_id

    return jsonify(result)


# ---------------------------------------------------- indian railways data --
@app.route("/api/rail-data/status", methods=["GET"])
def rail_data_status():
    return jsonify(railways_data.get_status())


@app.route("/api/rail-data/load", methods=["POST"])
def rail_data_load():
    conn = None
    try:
        conn = db.get_conn()
        status = railways_data.load_pipeline(conn)
        try:
            db.log_activity(
                conn, "system", "load_rail_data",
                f"Loaded {status['n_stations']} stations / {status['n_schedule_stops']} schedule stops "
                f"(source: {status['source']})",
            )
            conn.commit()
        except Exception:
            pass
        return jsonify(status)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@app.route("/api/rail-data/stations", methods=["GET"])
def rail_data_stations():
    zone = request.args.get("zone") or None
    search = request.args.get("search") or None
    conn = db.get_conn()
    rows = railways_data.list_stations(conn, zone=zone, search=search)
    conn.close()
    return jsonify(rows)


# ------------------------------------------------------------------- log --
@app.route("/api/activity", methods=["GET"])
def activity():
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT 40").fetchall()
    conn.close()
    return jsonify(rows_to_list(rows))


@app.route("/api/reset-demo", methods=["POST"])
def reset_demo():
    db.init_db(reset=True)
    return jsonify({"status": "reset"})


if __name__ == "__main__":
    db.init_db()
    port = int(os.environ.get("PORT", 5050))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug, use_reloader=False)

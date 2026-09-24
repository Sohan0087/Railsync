"""
RailSync optimization engine.

Two stages, both deliberately transparent/explainable (not a black box):

1. PRIORITY SCORING  — ranks pending maintenance requests using a weighted
   model over asset criticality, defect severity, asset condition and how
   long the request has been waiting. Produces a 0-100 score plus a
   breakdown so the recommendation can be explained to a human reviewer.

2. WINDOW OPTIMIZATION — for each request (highest priority first), scans
   the train timetable on that asset's line to find possession windows
   (gaps between train movements, expanded by the relevant safety buffer)
   long enough for the job, checks a matching crew/machine resource is
   free for that window, and avoids double-booking the line or the
   resource against anything already scheduled in this run. If nothing
   fits in the next 14 days it is flagged as a conflict for a human to
   resolve manually.
"""
import json
from datetime import datetime, timedelta

DAY_MINUTES = 24 * 60

ASSET_BUFFER_RULE = {
    "Switch": "Junction Possession Buffer",
    "Signal": "Signal Work Buffer",
    "Overhead Line": "OHLE Isolation Buffer",
}
DEFAULT_BUFFER_RULE = "Standard Possession Buffer"

ASSET_SKILL_MAP = {
    "Switch": "Switch",
    "Signal": "Signal",
    "Overhead Line": "Electrical",
    "Track": "Track",
    "Bridge": "Structural",
    "Platform": "Structural",
}


def _to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _to_hhmm(mins: int) -> str:
    mins = mins % DAY_MINUTES
    return f"{mins // 60:02d}:{mins % 60:02d}"


def compute_priority(request_row, asset_row):
    """Weighted, explainable priority score (0-100)."""
    criticality = asset_row["criticality"]                # 1-5
    severity = request_row["defect_severity"]              # 1-5
    condition = asset_row["condition_score"]                # 0-100 (higher=better)
    reported = datetime.fromisoformat(request_row["reported_date"]).date()
    waiting_days = max(0, (datetime.now().date() - reported).days)

    crit_component = (criticality / 5) * 30
    sev_component = (severity / 5) * 35
    condition_component = ((100 - condition) / 100) * 20
    waiting_component = min(waiting_days / 14, 1.0) * 15

    total = round(crit_component + sev_component + condition_component + waiting_component, 1)

    breakdown = {
        "asset_criticality": {"value": criticality, "weight": "30%", "points": round(crit_component, 1)},
        "defect_severity": {"value": severity, "weight": "35%", "points": round(sev_component, 1)},
        "asset_condition": {"value": condition, "weight": "20%", "points": round(condition_component, 1)},
        "waiting_days": {"value": waiting_days, "weight": "15%", "points": round(waiting_component, 1)},
        "total": total,
    }
    return total, breakdown


def _buffer_for_asset(conn, asset_type, line):
    rule_name = ASSET_BUFFER_RULE.get(asset_type, DEFAULT_BUFFER_RULE)
    row = conn.execute(
        "SELECT min_buffer_minutes FROM safety_constraints WHERE name = ? LIMIT 1",
        (rule_name,),
    ).fetchone()
    base = row["min_buffer_minutes"] if row else 20
    return base, rule_name


def _occupied_intervals(conn, line, buffer_minutes):
    """Train movement intervals for a line, expanded by the safety buffer, merged."""
    trains = conn.execute(
        "SELECT departure, arrival, priority FROM train_schedules WHERE line = ? ORDER BY departure",
        (line,),
    ).fetchall()
    raw = []
    for t in trains:
        dep = _to_min(t["departure"]) - buffer_minutes
        arr = _to_min(t["arrival"]) + buffer_minutes
        dep = max(dep, 0)
        arr = min(arr, DAY_MINUTES)
        if arr > dep:
            raw.append([dep, arr])
    raw.sort()
    merged = []
    for start, end in raw:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _gaps_for_day(occupied, booked_today):
    """Free intervals within one 24h day, after subtracting train occupancy
    AND anything already booked for that line/day in this optimization run."""
    blocks = sorted(occupied + booked_today)
    merged = []
    for start, end in blocks:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    gaps = []
    cursor = 0
    for start, end in merged:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < DAY_MINUTES:
        gaps.append((cursor, DAY_MINUTES))

    # Overnight wrap gap: from end of last block today to start of first block
    # (the schedule repeats daily, so "tomorrow's first block" starts at the
    # same minute as today's first block).
    if merged:
        first_start = merged[0][0]
        last_end = merged[-1][1]
        if last_end < DAY_MINUTES or first_start > 0:
            wrap_len = (DAY_MINUTES - last_end) + first_start
            if wrap_len > 0:
                gaps.append((last_end, DAY_MINUTES + first_start))  # represented on a 2-day timeline
    return gaps


def _disruption_score(start_min, duration, gap_len):
    """0-100, lower is better. Blends time-of-day (overnight is best) and
    how tight the fit is (more spare margin is safer)."""
    hour = (start_min % DAY_MINUTES) / 60
    time_factor = abs(((hour - 2 + 12) % 24) - 12) / 12  # 0 at 02:00, 1 at 14:00
    spare = max(0, gap_len - duration)
    tightness_factor = max(0, 1 - spare / max(duration, 1))
    score = 0.7 * time_factor + 0.3 * tightness_factor
    return round(min(score, 1.0) * 100, 1)


def _shift_window_ok(resource, day_start_abs, day_end_abs, day_offset):
    """Check the resource's daily shift covers [day_start_abs, day_end_abs)
    on an absolute multi-day minute timeline, handling overnight shifts."""
    shift_start = _to_min(resource["shift_start"])
    shift_end = _to_min(resource["shift_end"])
    base = day_offset * DAY_MINUTES
    if shift_end <= shift_start:  # overnight shift, e.g. 22:00 -> 05:00
        s_abs = base + shift_start
        e_abs = base + shift_end + DAY_MINUTES
    else:
        s_abs = base + shift_start
        e_abs = base + shift_end
    return s_abs <= day_start_abs and day_end_abs <= e_abs


def optimize(conn, horizon_days=14, only_status=("Pending", "Optimized")):
    """
    Runs the full pipeline: score every open request, then place each one
    (highest priority first) into the earliest low-disruption window that
    doesn't conflict with the line's train service, safety buffers, or a
    resource that's already committed elsewhere in this run.

    Requests already 'Approved' / 'In Progress' / 'Completed' are treated
    as fixed — their windows stay booked and are not reassigned, which is
    what lets re-optimize() safely shuffle only the still-pending work.
    """
    placeholders = ",".join("?" * len(only_status))
    open_requests = conn.execute(
        f"""SELECT mr.*, a.name as asset_name, a.asset_type, a.line, a.criticality,
                   a.condition_score
            FROM maintenance_requests mr
            JOIN assets a ON a.id = mr.asset_id
            WHERE mr.status IN ({placeholders})""",
        only_status,
    ).fetchall()

    fixed = conn.execute(
        """SELECT mr.*, a.line FROM maintenance_requests mr
           JOIN assets a ON a.id = mr.asset_id
           WHERE mr.status IN ('Approved','In Progress') AND mr.recommended_date IS NOT NULL"""
    ).fetchall()

    resources = conn.execute("SELECT * FROM resources WHERE status = 'Available'").fetchall()

    scored = []
    for r in open_requests:
        score, breakdown = compute_priority(r, r)
        scored.append((score, breakdown, r))
    scored.sort(key=lambda x: x[0], reverse=True)

    today = datetime.now().date()

    # line -> date_str -> list of [start_abs, end_abs] booked minute intervals
    line_bookings = {}
    # resource_id -> date_str -> list of [start_abs, end_abs]
    resource_bookings = {}

    def register_fixed():
        for f in fixed:
            if not f["recommended_date"]:
                continue
            line_bookings.setdefault(f["line"], {}).setdefault(f["recommended_date"], []).append(
                [_to_min(f["recommended_start"]), _to_min(f["recommended_end"])]
            )
            if f["recommended_resource_id"]:
                resource_bookings.setdefault(f["recommended_resource_id"], {}).setdefault(
                    f["recommended_date"], []
                ).append([_to_min(f["recommended_start"]), _to_min(f["recommended_end"])])

    register_fixed()

    results = []
    for score, breakdown, r in scored:
        buffer_minutes, rule_name = _buffer_for_asset(conn, r["asset_type"], r["line"])
        occupied = _occupied_intervals(conn, r["line"], buffer_minutes)
        duration = r["duration_minutes"]
        needed_skill = ASSET_SKILL_MAP.get(r["asset_type"])
        candidate_resources = [res for res in resources if res["skill"] == needed_skill] or list(resources)

        placed = False
        explanation_lines = [
            f"Priority {score}/100 (criticality {r['criticality']}/5, severity {r['defect_severity']}/5, "
            f"condition {r['condition_score']}/100, waiting {breakdown['waiting_days']['value']}d).",
            f"Safety rule applied: {rule_name} ({buffer_minutes} min clear buffer around train movements).",
        ]

        for day_offset in range(1, horizon_days + 1):
            date_str = str(today + timedelta(days=day_offset))
            booked_today = line_bookings.get(r["line"], {}).get(date_str, [])
            gaps = _gaps_for_day(occupied, booked_today)
            # sort candidate gaps by disruption score (best first)
            scored_gaps = []
            for g_start, g_end in gaps:
                g_len = g_end - g_start
                if g_len < duration:
                    continue
                start_abs = g_start
                end_abs = start_abs + duration
                d_score = _disruption_score(start_abs, duration, g_len)
                scored_gaps.append((d_score, start_abs, end_abs, g_len))
            scored_gaps.sort(key=lambda x: x[0])

            for d_score, start_abs, end_abs, g_len in scored_gaps:
                for res in candidate_resources:
                    r_bookings = resource_bookings.get(res["id"], {}).get(date_str, [])
                    overlap = any(not (end_abs <= b[0] or start_abs >= b[1]) for b in r_bookings)
                    if overlap:
                        continue
                    if not _shift_window_ok(res, start_abs, end_abs, 0):
                        continue

                    # commit booking
                    line_bookings.setdefault(r["line"], {}).setdefault(date_str, []).append([start_abs, end_abs])
                    resource_bookings.setdefault(res["id"], {}).setdefault(date_str, []).append([start_abs, end_abs])

                    display_date = today + timedelta(days=day_offset)
                    if start_abs >= DAY_MINUTES:  # wrapped into the following calendar day
                        display_date = today + timedelta(days=day_offset + 1)
                    start_hhmm = _to_hhmm(start_abs)
                    end_hhmm = _to_hhmm(end_abs)

                    explanation_lines.append(
                        f"Window found on {date_str} at {start_hhmm}-{end_hhmm} "
                        f"({g_len} min gap available for a {duration} min job, disruption {d_score}/100)."
                    )
                    explanation_lines.append(f"Assigned to {res['name']} ({res['resource_type']}, shift {res['shift_start']}-{res['shift_end']}).")

                    results.append({
                        "id": r["id"],
                        "priority_score": score,
                        "priority_breakdown": breakdown,
                        "recommended_date": date_str,
                        "recommended_start": start_hhmm,
                        "recommended_end": end_hhmm,
                        "recommended_resource_id": res["id"],
                        "disruption_score": d_score,
                        "explanation": " ".join(explanation_lines),
                        "conflict": None,
                        "status": "Optimized",
                    })
                    placed = True
                    break
                if placed:
                    break
            if placed:
                break

        if not placed:
            explanation_lines.append(
                f"No conflict-free window found within {horizon_days} days — line capacity or matching "
                f"resource availability is exhausted. Needs manual scheduling or resource reassignment."
            )
            results.append({
                "id": r["id"],
                "priority_score": score,
                "priority_breakdown": breakdown,
                "recommended_date": None,
                "recommended_start": None,
                "recommended_end": None,
                "recommended_resource_id": None,
                "disruption_score": None,
                "explanation": " ".join(explanation_lines),
                "conflict": "No available window/resource within horizon",
                "status": "Optimized",
            })

    return results


def apply_results(conn, results):
    for res in results:
        conn.execute(
            """UPDATE maintenance_requests SET
               priority_score=?, priority_breakdown=?, recommended_date=?,
               recommended_start=?, recommended_end=?, recommended_resource_id=?,
               disruption_score=?, explanation=?, conflict=?, status=?
               WHERE id=?""",
            (
                res["priority_score"], json.dumps(res["priority_breakdown"]), res["recommended_date"],
                res["recommended_start"], res["recommended_end"], res["recommended_resource_id"],
                res["disruption_score"], res["explanation"], res["conflict"], res["status"],
                res["id"],
            ),
        )
    conn.commit()

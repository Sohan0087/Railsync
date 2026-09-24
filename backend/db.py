"""
RailSync database layer.
SQLite schema + connection helpers. No ORM — plain sqlite3 with row factory,
kept deliberately transparent so the scheduling logic in optimizer.py is easy to audit.
"""
import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "railsync.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    asset_type TEXT NOT NULL,          -- Track, Signal, Switch, Overhead Line, Bridge, Platform
    line TEXT NOT NULL,                -- rail line / corridor it belongs to
    location_km REAL,                  -- chainage / km marker
    criticality INTEGER NOT NULL,      -- 1 (low) - 5 (critical, e.g. mainline junction)
    condition_score REAL NOT NULL,     -- 0-100, higher = better condition
    last_maintenance TEXT,             -- ISO date
    status TEXT NOT NULL DEFAULT 'Operational', -- Operational, Degraded, Out of Service
    ml_defect_probability REAL,        -- latest AI inspection result, 0-1
    last_inspection TEXT               -- ISO datetime of latest AI inspection
);

CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    resource_type TEXT NOT NULL,       -- Crew, Machine, Inspection Team
    skill TEXT,                        -- e.g. Track, Signal, Electrical, OHLE
    shift_start TEXT NOT NULL,         -- "HH:MM"
    shift_end TEXT NOT NULL,           -- "HH:MM"
    status TEXT NOT NULL DEFAULT 'Available'  -- Available, On Job, Off Duty
);

CREATE TABLE IF NOT EXISTS train_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    train_no TEXT NOT NULL,
    line TEXT NOT NULL,
    departure TEXT NOT NULL,           -- "HH:MM"
    arrival TEXT NOT NULL,             -- "HH:MM"
    day_type TEXT NOT NULL DEFAULT 'Daily', -- Daily, Weekday, Weekend
    priority TEXT NOT NULL DEFAULT 'Express' -- Express, Passenger, Freight
);

CREATE TABLE IF NOT EXISTS safety_constraints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    line TEXT,                          -- NULL = applies to all lines
    min_buffer_minutes INTEGER NOT NULL, -- required clear buffer around any train movement
    description TEXT
);

CREATE TABLE IF NOT EXISTS maintenance_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    defect_severity INTEGER NOT NULL,   -- 1-5, 5 = safety critical
    duration_minutes INTEGER NOT NULL,  -- required maintenance block length
    reported_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Pending', -- Pending, Optimized, Approved, In Progress, Completed, Rejected
    priority_score REAL,
    priority_breakdown TEXT,            -- JSON explanation of the score
    recommended_date TEXT,
    recommended_start TEXT,
    recommended_end TEXT,
    recommended_resource_id INTEGER,
    disruption_score REAL,
    explanation TEXT,
    conflict TEXT,                      -- non-null if a conflict was detected
    approved_by TEXT,
    FOREIGN KEY(asset_id) REFERENCES assets(id),
    FOREIGN KEY(recommended_resource_id) REFERENCES resources(id)
);

CREATE TABLE IF NOT EXISTS ir_stations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    state TEXT,
    zone TEXT,
    latitude REAL,
    longitude REAL
);

CREATE TABLE IF NOT EXISTS ir_schedule_stops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    train_no TEXT NOT NULL,
    train_name TEXT,
    station_code TEXT NOT NULL,
    arrival TEXT,
    departure TEXT,
    day INTEGER,
    distance REAL
);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT
);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(reset=False):
    if reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    fresh = not os.path.exists(DB_PATH)
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    if fresh:
        seed(conn)
    conn.close()


def log_activity(conn, actor, action, details=""):
    conn.execute(
        "INSERT INTO activity_log (ts, actor, action, details) VALUES (?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), actor, action, details),
    )


def seed(conn):
    cur = conn.cursor()

    assets = [
        ("Junction Points 14A", "Switch", "North Corridor", 12.4, 5, 62, "2026-06-02"),
        ("Overhead Line Section 7", "Overhead Line", "North Corridor", 18.1, 4, 55, "2026-05-14"),
        ("Rail Track Segment 22", "Track", "North Corridor", 22.6, 3, 71, "2026-07-01"),
        ("Signal Gantry S-9", "Signal", "North Corridor", 9.8, 5, 48, "2026-04-20"),
        ("Platform 3 Edge", "Platform", "Central Line", 3.2, 2, 84, "2026-03-11"),
        ("Rail Track Segment 5", "Track", "Central Line", 6.7, 3, 66, "2026-06-18"),
        ("Bridge Deck B-12", "Bridge", "Central Line", 14.9, 5, 58, "2026-02-27"),
        ("Signal Gantry S-14", "Signal", "Central Line", 16.3, 4, 73, "2026-07-05"),
        ("Junction Points 3B", "Switch", "South Corridor", 4.5, 4, 60, "2026-05-30"),
        ("Overhead Line Section 2", "Overhead Line", "South Corridor", 8.2, 3, 77, "2026-06-25"),
        ("Rail Track Segment 31", "Track", "South Corridor", 27.4, 3, 52, "2026-04-09"),
        ("Platform 1 Edge", "Platform", "South Corridor", 1.1, 2, 88, "2026-01-15"),
        ("Signal Gantry S-2", "Signal", "South Corridor", 5.9, 5, 44, "2026-03-22"),
        ("Rail Track Segment 9", "Track", "North Corridor", 33.0, 3, 69, "2026-07-10"),
        ("Junction Points 21C", "Switch", "Central Line", 11.0, 4, 63, "2026-05-05"),
    ]
    cur.executemany(
        """INSERT INTO assets (name, asset_type, line, location_km, criticality,
           condition_score, last_maintenance) VALUES (?,?,?,?,?,?,?)""",
        assets,
    )

    resources = [
        ("Track Crew Alpha", "Crew", "Track", "22:00", "05:00", "Available"),
        ("Track Crew Bravo", "Crew", "Track", "23:00", "06:00", "Available"),
        ("Signal Team North", "Crew", "Signal", "21:30", "04:30", "Available"),
        ("Signal Team South", "Crew", "Signal", "22:30", "05:30", "Available"),
        ("OHLE Maintenance Unit", "Machine", "Electrical", "00:00", "05:00", "Available"),
        ("Tamping Machine T-3", "Machine", "Track", "23:00", "04:00", "Available"),
        ("Bridge Inspection Team", "Inspection Team", "Structural", "21:00", "05:00", "Available"),
        ("Switch & Points Crew", "Crew", "Switch", "22:00", "05:30", "Available"),
    ]
    cur.executemany(
        """INSERT INTO resources (name, resource_type, skill, shift_start, shift_end, status)
           VALUES (?,?,?,?,?,?)""",
        resources,
    )

    # Representative daily train schedule per line (used to find maintenance gaps)
    lines = ["North Corridor", "Central Line", "South Corridor"]
    schedules = []
    base_times = [
        ("05:10", "05:40"), ("06:00", "06:35"), ("06:50", "07:20"), ("07:15", "07:50"),
        ("08:00", "08:35"), ("08:40", "09:10"), ("09:30", "10:05"), ("11:00", "11:35"),
        ("12:15", "12:50"), ("13:40", "14:15"), ("15:05", "15:40"), ("16:20", "16:55"),
        ("17:00", "17:35"), ("17:45", "18:20"), ("18:30", "19:05"), ("19:15", "19:50"),
        ("20:10", "20:45"), ("21:20", "21:55"), ("22:40", "23:10"),
    ]
    priorities = ["Express", "Passenger", "Passenger", "Freight"]
    for i, line in enumerate(lines):
        for j, (dep, arr) in enumerate(base_times):
            schedules.append((
                f"{line[0]}{100+i*30+j}", line, dep, arr, "Daily", priorities[j % 4]
            ))
    cur.executemany(
        """INSERT INTO train_schedules (train_no, line, departure, arrival, day_type, priority)
           VALUES (?,?,?,?,?,?)""",
        schedules,
    )

    constraints = [
        ("Standard Possession Buffer", None, 20, "Minimum clear buffer before/after any train movement for track access."),
        ("Signal Work Buffer", None, 30, "Extra buffer required when isolating signalling equipment."),
        ("OHLE Isolation Buffer", None, 35, "Overhead line isolation requires extended clearance either side."),
        ("Junction Possession Buffer", None, 25, "Points/switch work near junctions needs wider clearance for route conflicts."),
    ]
    cur.executemany(
        """INSERT INTO safety_constraints (name, line, min_buffer_minutes, description)
           VALUES (?,?,?,?)""",
        constraints,
    )

    today = datetime.now().date()
    requests = [
        (1, "Worn switch blade replacement", "Visual inspection found wear beyond tolerance on points blade.", 5, 90, str(today - timedelta(days=2))),
        (2, "Overhead line insulator crack", "Thermal imaging flagged a hairline crack on section 7 insulator.", 4, 120, str(today - timedelta(days=1))),
        (3, "Rail surface defect grinding", "Ultrasonic scan detected micro-fractures requiring grinding.", 3, 150, str(today - timedelta(days=4))),
        (4, "Signal aspect intermittent fault", "Driver reports flickering aspect at gantry S-9 during night runs.", 5, 75, str(today)),
        (7, "Bridge deck expansion joint wear", "Routine structural inspection found joint gap exceeding spec.", 5, 180, str(today - timedelta(days=3))),
        (9, "Points motor lubrication overdue", "Scheduled preventive maintenance overdue by 3 weeks.", 2, 45, str(today - timedelta(days=6))),
        (11, "Track geometry deviation", "Track recording car flagged gauge deviation near km 27.4.", 4, 110, str(today - timedelta(days=1))),
        (13, "Signal relay room humidity alert", "Environmental sensor triggered high-humidity alert near relay room.", 3, 60, str(today - timedelta(days=2))),
        (6, "Ballast fouling section 5", "Drainage inspection found ballast fouling reducing track support.", 2, 100, str(today - timedelta(days=5))),
        (10, "OHLE tensioning check", "Routine tension check due after temperature swing.", 2, 70, str(today - timedelta(days=7))),
    ]
    cur.executemany(
        """INSERT INTO maintenance_requests
           (asset_id, title, description, defect_severity, duration_minutes, reported_date)
           VALUES (?,?,?,?,?,?)""",
        requests,
    )

    log_activity(conn, "system", "seed", "Database seeded with sample assets, resources, schedules and requests.")
    conn.commit()

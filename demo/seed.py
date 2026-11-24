"""Create and seed ironvale.db (SQLite). Deterministic.

    python seed.py
"""
from __future__ import annotations

import os
import random
import sqlite3
from datetime import datetime, timedelta

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ironvale.db")
SEED = 20260801
START = datetime(2026, 8, 1)
DAYS = 92  # 1 Aug - 31 Oct 2026

MACHINES = [
    ("CNC-L1-01", "Line 1", "cnc_machining", "DMC 850", "2019-03-12"),
    ("CNC-L1-02", "Line 1", "cnc_machining", "DMC 850", "2019-03-12"),
    ("CNC-L1-03", "Line 1", "cnc_machining", "DMC 850", "2020-07-01"),
    ("CNC-L1-04", "Line 1", "cnc_machining", "DMC 850", "2020-07-01"),
    ("CNC-L1-05", "Line 1", "cnc_machining", "DMC 850", "2022-01-20"),
    ("CNC-L1-06", "Line 1", "cnc_machining", "DMC 850", "2023-05-08"),
    ("FUR-L2-01", "Line 2", "heat_treatment", "BF-1200", "2018-11-05"),
    ("FUR-L2-02", "Line 2", "heat_treatment", "BF-1200", "2021-09-14"),
    ("PRS-L3-01", "Line 3", "assembly_press", "HP-250", "2020-02-17"),
    ("LKT-L3-01", "Line 3", "leak_test", "LT-400", "2021-06-30"),
]

USERS = [
    ("u_sup_l1", "Priya Natarajan", "line_supervisor", "Line 1"),
    ("u_sup_l3", "Karthik Selvam", "line_supervisor", "Line 3"),
    ("u_qe", "Farah Qureshi", "quality_engineer", "All lines"),
    ("u_head", "Vikram Rao", "plant_head", "Whole plant"),
]

SUPPLIER_PARTS = [
    ("HX-220-B", "Arcflow Castings", 412.0, "2026-07-01"),
    ("HX-180-A", "Arcflow Castings", 356.0, "2026-07-01"),
    ("BK-310-C", "Shakti Forgings", 588.0, "2026-07-01"),
    ("FS-M8-FL", "Metsun Fasteners", 9.0, "2026-07-01"),
    ("SL-220-R", "Tamron Seals", 63.0, "2026-07-01"),
]

LINE_PARTS = {
    "Line 1": ["HX-220-B", "HX-180-A", "BK-310-C"],
    "Line 2": ["HX-220-B", "HX-180-A"],
    "Line 3": ["HX-220-B", "FS-M8-FL"],
}
CATEGORIES = ["planned", "breakdown", "changeover", "material"]
CNC_ALARMS = ["E-417", "E-471", "E-401", "E-514", "E-630"]
DEFECTS = ["bore_oversize", "surface_finish", "cracked_housing", "leak_fail", "low_hardness"]

SCHEMA = """
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS machines;
DROP TABLE IF EXISTS work_orders;
DROP TABLE IF EXISTS downtime_events;
DROP TABLE IF EXISTS defects;
DROP TABLE IF EXISTS supplier_parts;

CREATE TABLE users (
    user_id TEXT PRIMARY KEY, name TEXT, role TEXT, scope TEXT);
CREATE TABLE machines (
    machine_id TEXT PRIMARY KEY, line TEXT, type TEXT, model TEXT,
    commissioned_on TEXT);
CREATE TABLE work_orders (
    wo_id TEXT PRIMARY KEY, machine_id TEXT, part_no TEXT,
    qty_planned INTEGER, qty_good INTEGER, started_at TEXT, ended_at TEXT);
CREATE TABLE downtime_events (
    event_id TEXT PRIMARY KEY, machine_id TEXT, start_at TEXT, end_at TEXT,
    category TEXT, reason_code TEXT, alarm_code TEXT);
CREATE TABLE defects (
    defect_id TEXT PRIMARY KEY, wo_id TEXT, part_no TEXT, defect_type TEXT,
    qty INTEGER, found_at TEXT, stage TEXT);
CREATE TABLE supplier_parts (
    part_no TEXT, supplier TEXT, unit_price_inr REAL, valid_from TEXT);
"""


def main() -> None:
    rng = random.Random(SEED)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)

    con.executemany("INSERT INTO users VALUES (?,?,?,?)", USERS)
    con.executemany("INSERT INTO machines VALUES (?,?,?,?,?)", MACHINES)
    con.executemany("INSERT INTO supplier_parts VALUES (?,?,?,?)", SUPPLIER_PARTS)

    line_of = {m[0]: m[1] for m in MACHINES}

    # ---- work orders
    work_orders, defects = [], []
    for i in range(1, 401):
        mid = rng.choice(MACHINES)[0]
        part = rng.choice(LINE_PARTS[line_of[mid]])
        start = START + timedelta(days=rng.randrange(DAYS), hours=rng.randrange(6, 20))
        planned = rng.choice([40, 60, 80, 100, 120])
        good = int(planned * rng.uniform(0.90, 1.0))
        wo = f"WO-2026-{i:05d}"
        work_orders.append((wo, mid, part, planned, good,
                            start.isoformat(sep=" "),
                            (start + timedelta(hours=rng.uniform(2, 7))).isoformat(sep=" ")))
        if planned - good > 0 and rng.random() < 0.6:
            defects.append((
                f"DEF-2026-{len(defects)+1:05d}", wo, part, rng.choice(DEFECTS),
                rng.randint(1, max(1, planned - good)),
                (start + timedelta(hours=2)).isoformat(sep=" "), "inline",
            ))

    # Guaranteed: cracked HX-220-B on CNC-L1-04 in September 2026 (QDR-2026-014).
    for d in range(4, 26, 3):
        start = datetime(2026, 9, d, 6, 30)
        wo = f"WO-2026-9{d:04d}"
        work_orders.append((wo, "CNC-L1-04", "HX-220-B", 120, 104,
                            start.isoformat(sep=" "),
                            (start + timedelta(hours=6)).isoformat(sep=" ")))
        defects.append((
            f"DEF-2026-9{d:04d}", wo, "HX-220-B", "cracked_housing",
            rng.randint(3, 12), (start + timedelta(hours=2)).isoformat(sep=" "),
            "machining",
        ))

    con.executemany("INSERT INTO work_orders VALUES (?,?,?,?,?,?,?)", work_orders)
    con.executemany("INSERT INTO defects VALUES (?,?,?,?,?,?,?)", defects)

    # ---- downtime
    events = []

    def add(mid, start, minutes, category, alarm=None, reason=None):
        events.append((
            f"DT-2026-{len(events)+1:05d}", mid, start.isoformat(sep=" "),
            (start + timedelta(minutes=minutes)).isoformat(sep=" "),
            category, reason or f"{category}_generic", alarm,
        ))

    for _ in range(600):
        mid = rng.choice(MACHINES)[0]
        cat = rng.choice(CATEGORIES)
        start = START + timedelta(days=rng.randrange(DAYS), hours=rng.randrange(6, 22))
        if cat == "changeover" and line_of[mid] == "Line 1":
            mins = max(18, min(70, rng.gauss(35, 6)))      # SOP-L1-001 target 35
        elif cat == "breakdown":
            mins = max(10, min(300, rng.lognormvariate(3.9, 0.7)))
        else:
            mins = rng.choice([30, 45, 60, 90, 120])
        alarm = (rng.choice(CNC_ALARMS) if cat == "breakdown" and mid.startswith("CNC")
                 else None)
        add(mid, start, round(mins, 1), cat, alarm)

    # Guaranteed: October breakdowns on all 6 Line 1 machines -> 6 bars.
    for i, m in enumerate([x[0] for x in MACHINES if x[1] == "Line 1"]):
        for k in range(3):
            day = 2 + (i * 4 + k * 7) % 27
            add(m, datetime(2026, 10, day, 7 + k * 4, 15),
                round(rng.uniform(25, 180), 1), "breakdown",
                "E-417" if k == 0 else rng.choice(CNC_ALARMS), "coolant_fault")

    # Guaranteed: Line 3 has October downtime, so a Line 3 question is
    # not_found by row scope, not by absence of data.
    for i, m in enumerate(["PRS-L3-01", "LKT-L3-01"]):
        for k in range(4):
            day = 3 + (i * 5 + k * 6) % 26
            add(m, datetime(2026, 10, day, 9 + k * 3, 40),
                round(rng.uniform(20, 150), 1), "breakdown")

    con.executemany("INSERT INTO downtime_events VALUES (?,?,?,?,?,?,?)", events)
    con.commit()

    # ---- verify the facts the demo depends on
    q = con.execute
    checks = [
        ("Arcflow supplies HX-220-B at 412",
         q("SELECT unit_price_inr FROM supplier_parts WHERE part_no='HX-220-B'"
           " AND supplier='Arcflow Castings'").fetchone()[0] == 412.0),
        ("6 Line 1 machines have Oct breakdowns",
         q("SELECT COUNT(DISTINCT d.machine_id) FROM downtime_events d"
           " JOIN machines m ON m.machine_id=d.machine_id WHERE m.line='Line 1'"
           " AND d.category='breakdown' AND d.start_at>='2026-10-01'"
           " AND d.start_at<'2026-11-01'").fetchone()[0] == 6),
        ("Line 3 has October downtime",
         q("SELECT COUNT(*) FROM downtime_events d JOIN machines m"
           " ON m.machine_id=d.machine_id WHERE m.line='Line 3'"
           " AND d.start_at>='2026-10-01' AND d.start_at<'2026-11-01'"
           ).fetchone()[0] > 0),
        ("cracked HX-220-B on CNC-L1-04 in Sept",
         q("SELECT COUNT(*) FROM defects d JOIN work_orders w ON w.wo_id=d.wo_id"
           " WHERE d.defect_type='cracked_housing' AND w.machine_id='CNC-L1-04'"
           " AND d.found_at>='2026-09-01' AND d.found_at<'2026-10-01'"
           ).fetchone()[0] >= 5),
        ("E-417 events exist",
         q("SELECT COUNT(*) FROM downtime_events WHERE alarm_code='E-417'"
           ).fetchone()[0] > 0),
    ]
    print(f"seeded {DB}")
    print(f"  machines={len(MACHINES)} work_orders={len(work_orders)} "
          f"downtime={len(events)} defects={len(defects)}")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok &= passed
    con.close()
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()

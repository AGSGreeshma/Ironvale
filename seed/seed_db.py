"""Create the schema, seed deterministic operational data, then install the
role/view/RLS layer.

The seed is not just plausible - it has to agree with the corpus, because the
graph route and the eval both cross-check documents against rows:

  * Arcflow Castings supplies HX-220-B at unit_price_inr 412 (matches PRC-2026-Q3)
  * cracked HX-220-B housings on CNC-L1-04 in September 2026 (matches QDR-2026-014)
  * every one of the 6 Line 1 machines has October 2026 breakdown downtime,
    so G05 returns 6 bars
  * some downtime rows carry alarm_code E-417, on DMC 850 machines only
  * Line 1 changeover events average close to 35 minutes (matches SOP-L1-001)
  * Line 3 has October downtime, so G06 is not_found because of row scope only

Those are asserted at the end of this script, so a drifting seed fails loudly.

    python seed/seed_db.py
"""
from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402

from app.config import settings  # noqa: E402

SEED = 20260801
START = datetime(2026, 8, 1)
END = datetime(2026, 11, 1)  # exclusive

MACHINES = [
    # machine_id,    line,     type,              model,         commissioned
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

LINE_PARTS = {
    "Line 1": ["HX-220-B", "HX-180-A", "BK-310-C"],
    "Line 2": ["HX-220-B", "HX-180-A", "BK-310-C"],
    "Line 3": ["HX-220-B", "HX-180-A"],
}

# Exactly the rows in PRC-2026-Q3, so the doc and the table cannot disagree.
SUPPLIER_PARTS = [
    ("HX-220-B", "Arcflow Castings", 412.00, "2026-07-01"),
    ("HX-180-A", "Arcflow Castings", 356.00, "2026-07-01"),
    ("HX-180-A", "Shakti Forgings", 349.00, "2026-07-01"),
    ("BK-310-C", "Shakti Forgings", 588.00, "2026-07-01"),
    ("FS-M8-FL", "Metsun Fasteners", 9.00, "2026-07-01"),
    ("FS-M10-FL", "Metsun Fasteners", 14.00, "2026-07-01"),
    ("SL-220-R", "Tamron Seals", 63.00, "2026-07-01"),
    ("SL-180-R", "Tamron Seals", 57.00, "2026-07-01"),
    ("BR-6208", "Velkon Bearings", 211.00, "2026-07-01"),
    ("BR-6210", "Velkon Bearings", 248.00, "2026-07-01"),
]

USERS = [
    ("u_sup_l1", "Priya Natarajan", "line_supervisor", "Line 1"),
    ("u_sup_l3", "Karthik Selvam", "line_supervisor", "Line 3"),
    ("u_qe", "Farah Qureshi", "quality_engineer", "All lines"),
    ("u_head", "Vikram Rao", "plant_head", "Whole plant"),
]

REASONS = {
    "planned": ["preventive_maintenance", "calibration", "shift_break", "trial_run"],
    "breakdown": ["spindle_fault", "coolant_fault", "interlock_fault", "drive_fault",
                  "sensor_fault", "hydraulic_fault", "conveyor_jam"],
    "changeover": ["part_change", "fixture_change", "tool_change"],
    "material": ["awaiting_castings", "awaiting_fasteners", "awaiting_seals",
                 "rework_queue", "awaiting_crane"],
}

# Alarm codes only ever attach to a machine that can raise them.
CNC_ALARMS = ["E-417", "E-471", "E-401", "E-423", "E-514", "E-526", "E-601", "E-630"]
FUR_ALARMS = ["F-210", "F-224", "F-237", "F-255", "F-268", "F-279"]
LKT_ALARMS = ["L-110", "L-140", "L-205", "L-230"]
PRS_ALARMS = ["P-120", "P-145", "P-210"]

DEFECT_TYPES = {
    "Line 1": ["bore_oversize", "surface_finish", "burr", "cracked_housing", "chatter_mark"],
    "Line 2": ["low_hardness", "distortion", "soft_spot", "scale"],
    "Line 3": ["leak_fail", "seal_damage", "press_depth", "torque_out_of_spec"],
}
STAGES = {"Line 1": "machining", "Line 2": "heat_treatment", "Line 3": "assembly"}


def _dt(rng: random.Random) -> datetime:
    """A random instant in the seed window, biased to working hours."""
    day = START + timedelta(days=rng.randrange((END - START).days))
    shift_start = rng.choice([6, 14, 22])
    return day + timedelta(
        hours=shift_start, minutes=rng.randrange(0, 480), seconds=rng.randrange(0, 60)
    )


def build_rows(rng: random.Random):
    machines_by_line = {}
    for m in MACHINES:
        machines_by_line.setdefault(m[1], []).append(m[0])

    # ---------------------------------------------------------- work orders
    work_orders = []
    wo_seq = 0
    n_wo_target = 1500
    while len(work_orders) < n_wo_target:
        mid, line = rng.choice([(m[0], m[1]) for m in MACHINES])
        part = rng.choice(LINE_PARTS[line])
        started = _dt(rng)
        dur = timedelta(hours=rng.uniform(1.5, 7.5))
        ended = started + dur
        planned = rng.choice([40, 60, 80, 100, 120, 150, 200])
        # Yield band 0.90-1.00, occasionally worse.
        ratio = rng.uniform(0.90, 1.0) if rng.random() > 0.08 else rng.uniform(0.70, 0.90)
        good = max(0, min(planned, int(round(planned * ratio))))
        wo_seq += 1
        work_orders.append(
            (f"WO-2026-{wo_seq:05d}", mid, part, planned, good, started, ended)
        )

    # Guarantee HX-220-B work orders on CNC-L1-04 in September 2026 so the
    # cracked-housing defects from QDR-2026-014 have somewhere to hang.
    sept_l104 = []
    for d in range(4, 28, 2):
        wo_seq += 1
        started = datetime(2026, 9, d, 6, 30)
        wo = (
            f"WO-2026-{wo_seq:05d}", "CNC-L1-04", "HX-220-B", 120,
            rng.randint(92, 110), started, started + timedelta(hours=6),
        )
        work_orders.append(wo)
        sept_l104.append(wo)

    # ------------------------------------------------------ downtime events
    downtime = []
    ev = 0

    def add_event(mid, line, start, minutes, category, reason=None, alarm=None):
        nonlocal ev
        ev += 1
        downtime.append(
            (
                f"DT-2026-{ev:05d}", mid, start, start + timedelta(minutes=minutes),
                category, reason or rng.choice(REASONS[category]), alarm,
            )
        )

    def alarm_for(mid, category):
        """Breakdowns carry an alarm code appropriate to the machine family."""
        if category != "breakdown":
            return None
        if rng.random() < 0.30:
            return None
        if mid.startswith("CNC"):
            # E-417 is over-weighted on purpose: it is the demo alarm.
            return rng.choice(CNC_ALARMS + ["E-417", "E-417"])
        if mid.startswith("FUR"):
            return rng.choice(FUR_ALARMS)
        if mid.startswith("LKT"):
            return rng.choice(LKT_ALARMS)
        return rng.choice(PRS_ALARMS)

    n_dt_target = 2000
    weights = [("planned", 0.24), ("breakdown", 0.22), ("changeover", 0.26), ("material", 0.28)]
    cats = [c for c, _ in weights]
    probs = [w for _, w in weights]

    while len(downtime) < n_dt_target - 40:  # leave room for the guaranteed rows
        mid, line = rng.choice([(m[0], m[1]) for m in MACHINES])
        category = rng.choices(cats, probs)[0]
        start = _dt(rng)
        if category == "changeover" and line == "Line 1":
            # SOP-L1-001 target is 35 minutes; keep the mean on it.
            minutes = max(18.0, min(70.0, rng.gauss(35.0, 6.0)))
        elif category == "changeover":
            minutes = max(15.0, min(90.0, rng.gauss(42.0, 10.0)))
        elif category == "breakdown":
            minutes = max(8.0, min(600.0, rng.lognormvariate(3.9, 0.75)))
        elif category == "planned":
            minutes = rng.choice([30.0, 45.0, 60.0, 90.0, 120.0, 240.0])
        else:
            minutes = max(10.0, min(480.0, rng.gauss(75.0, 45.0)))
        add_event(mid, line, start, round(minutes, 1), category, alarm=alarm_for(mid, category))

    # Guaranteed: every Line 1 machine has October 2026 breakdown downtime
    # (G05 must yield exactly 6 bars), and at least one E-417 among them.
    for i, mid in enumerate(machines_by_line["Line 1"]):
        for k in range(3):
            day = 2 + (i * 4 + k * 7) % 27
            start = datetime(2026, 10, day, 7 + k * 4, 15)
            alarm = "E-417" if k == 0 else rng.choice(CNC_ALARMS)
            add_event(
                mid, "Line 1", start, round(rng.uniform(25, 180), 1),
                "breakdown", "coolant_fault" if alarm == "E-417" else None, alarm,
            )

    # Guaranteed: Line 3 has October downtime, so a Line 3 question is
    # not_found for a Line 1 supervisor purely because of row scope.
    for i, mid in enumerate(machines_by_line["Line 3"]):
        for k in range(4):
            day = 3 + (i * 5 + k * 6) % 26
            start = datetime(2026, 10, day, 9 + k * 3, 40)
            add_event(mid, "Line 3", start, round(rng.uniform(20, 150), 1), "breakdown",
                      alarm=rng.choice(LKT_ALARMS if mid.startswith("LKT") else PRS_ALARMS))

    # ------------------------------------------------------------- defects
    defects = []
    dcount = 0
    line_of = {m[0]: m[1] for m in MACHINES}
    for wo in work_orders:
        wo_id, mid, part, planned, good, started, ended = wo
        shortfall = planned - good
        if shortfall <= 0 or rng.random() > 0.55:
            continue
        line = line_of[mid]
        n = rng.randint(1, 2)
        remaining = shortfall
        for _ in range(n):
            if remaining <= 0:
                break
            qty = rng.randint(1, max(1, remaining))
            remaining -= qty
            dcount += 1
            defects.append(
                (
                    f"DEF-2026-{dcount:05d}", wo_id, part,
                    rng.choice(DEFECT_TYPES[line]), qty,
                    started + timedelta(hours=rng.uniform(0.5, 6.0)), STAGES[line],
                )
            )

    # Guaranteed: cracked HX-220-B housings on CNC-L1-04 in September 2026.
    for wo in sept_l104:
        wo_id, mid, part, planned, good, started, ended = wo
        dcount += 1
        defects.append(
            (
                f"DEF-2026-{dcount:05d}", wo_id, "HX-220-B", "cracked_housing",
                rng.randint(3, 14), started + timedelta(hours=2.5), "machining",
            )
        )

    return work_orders, downtime, defects


# ---------------------------------------------------------------------------

def run_sql_file(cur, path: Path) -> None:
    cur.execute(path.read_text(encoding="utf-8"))


def main() -> int:
    rng = random.Random(SEED)
    work_orders, downtime, defects = build_rows(rng)

    here = Path(__file__).resolve().parent
    dsn = settings.pg_dsn()

    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            print("applying schema.sql ...")
            run_sql_file(cur, here / "schema.sql")

            print("inserting reference data ...")
            cur.executemany(
                "INSERT INTO machines (machine_id, line, type, model, commissioned_on)"
                " VALUES (%s, %s, %s, %s, %s)",
                MACHINES,
            )
            cur.executemany(
                "INSERT INTO users (user_id, name, role, scope) VALUES (%s, %s, %s, %s)",
                USERS,
            )
            cur.executemany(
                "INSERT INTO supplier_parts (part_no, supplier, unit_price_inr, valid_from)"
                " VALUES (%s, %s, %s, %s)",
                SUPPLIER_PARTS,
            )

            print(f"inserting {len(work_orders)} work orders ...")
            cur.executemany(
                "INSERT INTO work_orders (wo_id, machine_id, part_no, qty_planned,"
                " qty_good, started_at, ended_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                work_orders,
            )

            print(f"inserting {len(downtime)} downtime events ...")
            cur.executemany(
                "INSERT INTO downtime_events (event_id, machine_id, start_at, end_at,"
                " category, reason_code, alarm_code) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                downtime,
            )

            print(f"inserting {len(defects)} defects ...")
            cur.executemany(
                "INSERT INTO defects (defect_id, wo_id, part_no, defect_type, qty,"
                " found_at, stage) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                defects,
            )

            print("applying views_and_roles.sql ...")
            cur.execute(
                "SELECT set_config('ironask.role_password', %s, false)",
                (settings.pg_role_password,),
            )
            run_sql_file(cur, here / "views_and_roles.sql")

        conn.commit()

        # ------------------------------------------------- consistency checks
        print("\nverifying seed consistency ...")
        checks: list[tuple[str, bool, str]] = []
        with conn.cursor() as cur:

            def one(sql, params=None):
                cur.execute(sql, params or ())
                return cur.fetchone()[0]

            price = one(
                "SELECT unit_price_inr FROM supplier_parts"
                " WHERE part_no='HX-220-B' AND supplier='Arcflow Castings'"
            )
            checks.append(("Arcflow supplies HX-220-B at 412", float(price) == 412.00, str(price)))

            n = one(
                "SELECT count(*) FROM defects d JOIN work_orders w ON w.wo_id=d.wo_id"
                " WHERE d.part_no='HX-220-B' AND d.defect_type='cracked_housing'"
                " AND w.machine_id='CNC-L1-04'"
                " AND d.found_at >= '2026-09-01' AND d.found_at < '2026-10-01'"
            )
            checks.append(("cracked HX-220-B on CNC-L1-04 in Sept 2026", n >= 5, f"{n} defects"))

            n = one(
                "SELECT count(DISTINCT d.machine_id) FROM downtime_events d"
                " JOIN machines m ON m.machine_id=d.machine_id"
                " WHERE m.line='Line 1' AND d.category='breakdown'"
                " AND d.start_at >= '2026-10-01' AND d.start_at < '2026-11-01'"
            )
            checks.append(("all 6 Line 1 machines have Oct breakdowns", n == 6, f"{n} machines"))

            n = one(
                "SELECT count(*) FROM downtime_events d JOIN machines m"
                " ON m.machine_id=d.machine_id"
                " WHERE d.alarm_code='E-417' AND m.model='DMC 850'"
            )
            checks.append(("E-417 events exist on DMC 850", n >= 5, f"{n} events"))

            n = one("SELECT count(*) FROM downtime_events WHERE alarm_code='E-417'")
            m = one(
                "SELECT count(*) FROM downtime_events d JOIN machines m"
                " ON m.machine_id=d.machine_id"
                " WHERE d.alarm_code='E-417' AND m.model='DMC 850'"
            )
            checks.append(("E-417 only on DMC 850", n == m, f"{n} total / {m} on DMC 850"))

            avg = one(
                "SELECT avg(EXTRACT(EPOCH FROM (end_at-start_at))/60) FROM downtime_events d"
                " JOIN machines m ON m.machine_id=d.machine_id"
                " WHERE m.line='Line 1' AND d.category='changeover'"
            )
            checks.append((
                "Line 1 changeover averages near 35 min",
                33.0 <= float(avg) <= 37.0, f"{float(avg):.1f} min",
            ))

            n = one(
                "SELECT count(*) FROM downtime_events d JOIN machines m"
                " ON m.machine_id=d.machine_id"
                " WHERE m.line='Line 3'"
                " AND d.start_at >= '2026-10-01' AND d.start_at < '2026-11-01'"
            )
            checks.append(("Line 3 has October downtime", n >= 5, f"{n} events"))

            nwo = one("SELECT count(*) FROM work_orders")
            ndt = one("SELECT count(*) FROM downtime_events")
            ndf = one("SELECT count(*) FROM defects")
            checks.append(("~1500 work orders", 1400 <= nwo <= 1700, str(nwo)))
            checks.append(("~2000 downtime events", 1900 <= ndt <= 2100, str(ndt)))
            checks.append(("defects linked to work orders", ndf > 300, str(ndf)))

            n = one(
                "SELECT count(*) FROM defects d LEFT JOIN work_orders w ON w.wo_id=d.wo_id"
                " WHERE w.wo_id IS NULL"
            )
            checks.append(("no orphan defects", n == 0, f"{n} orphans"))

        ok = True
        for label, passed, detail in checks:
            print(f"  [{'PASS' if passed else 'FAIL'}] {label}  ({detail})")
            ok &= passed

        if not ok:
            print("\nseed consistency FAILED")
            return 1
        print("\nseed complete and consistent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

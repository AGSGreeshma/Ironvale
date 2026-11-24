"""Proof, at the SQL layer, that the role/view/RLS design actually holds.

These are the "prove with SQL tests" requirements:
  * a supervisor cannot read another line
  * a quality engineer cannot read unit_price_inr
  * none of the application roles can write

Plus the properties the rest of the design leans on: no base-table access at
all, and fail-closed behaviour when app.user_id is missing or unknown.
"""
from __future__ import annotations

import psycopg
import pytest

from app.db import ROLE_LOGIN, admin_conn, role_conn
from app.config import settings


def q(conn, sql, params=None):
    with conn.cursor() as cur:
        # Pass None, not (), when there are no parameters: an empty sequence
        # still turns on placeholder parsing and a LIKE '%' would blow up.
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        return cur.fetchall()


# ---------------------------------------------------------------- row scope

def test_line1_supervisor_sees_only_line_1_machines():
    with role_conn("line_supervisor", "u_sup_l1") as c:
        lines = {r[0] for r in q(c, "SELECT DISTINCT line FROM machines")}
    assert lines == {"Line 1"}


def test_line1_supervisor_cannot_read_line_3_machines():
    with role_conn("line_supervisor", "u_sup_l1") as c:
        rows = q(c, "SELECT machine_id FROM machines WHERE line = 'Line 3'")
        assert rows == []
        # Even naming the machine directly returns nothing.
        rows = q(c, "SELECT machine_id FROM machines WHERE machine_id = 'LKT-L3-01'")
        assert rows == []


def test_line1_supervisor_cannot_read_line_3_downtime():
    with role_conn("line_supervisor", "u_sup_l1") as c:
        rows = q(
            c,
            "SELECT count(*) FROM downtime_events WHERE machine_id IN ('PRS-L3-01','LKT-L3-01')",
        )
    assert rows[0][0] == 0


def test_line3_supervisor_sees_only_line_3():
    with role_conn("line_supervisor", "u_sup_l3") as c:
        lines = {r[0] for r in q(c, "SELECT DISTINCT line FROM machines")}
        assert lines == {"Line 3"}
        n = q(c, "SELECT count(*) FROM downtime_events WHERE machine_id LIKE 'CNC-L1%'")
        assert n[0][0] == 0


def test_line3_supervisor_has_october_downtime_in_scope():
    """G06 must be not_found because of row scope, not because data is absent."""
    with role_conn("line_supervisor", "u_sup_l3") as c:
        n = q(
            c,
            "SELECT count(*) FROM downtime_events"
            " WHERE start_at >= '2026-10-01' AND start_at < '2026-11-01'",
        )
    assert n[0][0] > 0


def test_qe_and_head_see_every_line():
    for role, uid in (("quality_engineer", "u_qe"), ("plant_head", "u_head")):
        with role_conn(role, uid) as c:
            lines = {r[0] for r in q(c, "SELECT DISTINCT line FROM machines")}
        assert lines == {"Line 1", "Line 2", "Line 3"}, role


# ------------------------------------------------------------ column scope

def test_qe_cannot_read_unit_price_inr():
    with role_conn("quality_engineer", "u_qe") as c:
        with pytest.raises(psycopg.errors.UndefinedColumn):
            q(c, "SELECT unit_price_inr FROM supplier_parts")


def test_qe_cannot_reach_price_by_alias_or_aggregate():
    for sql in (
        "SELECT sum(unit_price_inr) FROM supplier_parts",
        "SELECT s.unit_price_inr AS p FROM supplier_parts s",
        "SELECT * FROM supplier_parts WHERE unit_price_inr = 412",
    ):
        with role_conn("quality_engineer", "u_qe") as c:
            with pytest.raises(psycopg.Error):
                q(c, sql)


def test_qe_supplier_parts_projects_no_price_column():
    with role_conn("quality_engineer", "u_qe") as c:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM supplier_parts LIMIT 1")
            cols = [d.name for d in cur.description]
    assert "unit_price_inr" not in cols
    assert set(cols) == {"part_no", "supplier", "valid_from"}


def test_head_can_read_the_price():
    with role_conn("plant_head", "u_head") as c:
        rows = q(
            c,
            "SELECT unit_price_inr FROM supplier_parts"
            " WHERE part_no='HX-220-B' AND supplier='Arcflow Castings'",
        )
    assert float(rows[0][0]) == 412.00


def test_supervisor_has_no_defects_or_supplier_parts_at_all():
    for sql in ("SELECT 1 FROM defects", "SELECT 1 FROM supplier_parts"):
        with role_conn("line_supervisor", "u_sup_l1") as c:
            with pytest.raises(psycopg.Error):
                q(c, sql)


# ------------------------------------------------------------- no base tables

@pytest.mark.parametrize("role,uid", list(
    (r, u) for r, u in (("line_supervisor", "u_sup_l1"),
                        ("quality_engineer", "u_qe"),
                        ("plant_head", "u_head"))
))
def test_app_roles_cannot_touch_base_tables(role, uid):
    for table in ("public.machines", "public.work_orders", "public.downtime_events",
                  "public.defects", "public.supplier_parts", "public.users",
                  "public.doc_chunks", "public.documents"):
        with role_conn(role, uid) as c:
            with pytest.raises(psycopg.Error):
                q(c, f"SELECT 1 FROM {table} LIMIT 1")


@pytest.mark.parametrize("role,uid", [("line_supervisor", "u_sup_l1"),
                                      ("quality_engineer", "u_qe"),
                                      ("plant_head", "u_head")])
def test_app_roles_cannot_read_the_users_table(role, uid):
    """Role claims must be impossible to self-serve from inside a query."""
    with role_conn(role, uid) as c:
        with pytest.raises(psycopg.Error):
            q(c, "SELECT user_id, role, scope FROM public.users")


# -------------------------------------------------------------- no writes

WRITES = [
    "INSERT INTO machines (machine_id, line, type, model, commissioned_on)"
    " VALUES ('X-1','Line 9','t','m','2026-01-01')",
    "UPDATE machines SET line = 'Line 9'",
    "DELETE FROM machines",
    "INSERT INTO public.machines (machine_id, line, type, model, commissioned_on)"
    " VALUES ('X-2','Line 9','t','m','2026-01-01')",
    "UPDATE public.supplier_parts SET unit_price_inr = 1",
    "DROP TABLE public.machines",
    "CREATE TABLE sneaky (id int)",
    "TRUNCATE public.downtime_events",
]


@pytest.mark.parametrize("role,uid", [("line_supervisor", "u_sup_l1"),
                                      ("quality_engineer", "u_qe"),
                                      ("plant_head", "u_head")])
def test_app_roles_cannot_write(role, uid):
    for sql in WRITES:
        with role_conn(role, uid) as c:
            with pytest.raises(psycopg.Error):
                q(c, sql)


def test_row_counts_unchanged_after_write_attempts():
    with admin_conn() as conn:
        before = q(conn, "SELECT count(*) FROM machines")[0][0]
    for role, uid in (("line_supervisor", "u_sup_l1"), ("plant_head", "u_head")):
        for sql in WRITES:
            try:
                with role_conn(role, uid) as c:
                    q(c, sql)
            except psycopg.Error:
                pass
    with admin_conn() as conn:
        after = q(conn, "SELECT count(*) FROM machines")[0][0]
    assert before == after


# ------------------------------------------------------------- fail closed

def test_unset_user_id_returns_no_rows():
    """No app.user_id -> app_role() is NULL -> every policy predicate false."""
    login, _ = ROLE_LOGIN["plant_head"]
    dsn = settings.pg_dsn(user=login, password=settings.pg_role_password)
    with psycopg.connect(dsn, autocommit=False) as conn:
        assert q(conn, "SELECT count(*) FROM machines")[0][0] == 0
        assert q(conn, "SELECT count(*) FROM downtime_events")[0][0] == 0


def test_unknown_user_id_returns_no_rows():
    with role_conn("plant_head", "u_does_not_exist") as c:
        assert q(c, "SELECT count(*) FROM machines")[0][0] == 0


def test_user_id_cannot_borrow_another_roles_scope():
    """A supervisor login presenting the plant head's user_id still gets only
    what RLS allows for that user_id - and the view set stays the supervisor's,
    so there is no path to defects or supplier_parts."""
    with role_conn("line_supervisor", "u_head") as c:
        with pytest.raises(psycopg.Error):
            q(c, "SELECT 1 FROM supplier_parts")


def test_statement_timeout_is_five_seconds():
    for role, uid in (("line_supervisor", "u_sup_l1"), ("quality_engineer", "u_qe"),
                      ("plant_head", "u_head")):
        with role_conn(role, uid) as c:
            got = q(c, "SHOW statement_timeout")[0][0]
        assert got in ("5s", "5000ms"), (role, got)


def test_sessions_are_read_only_by_default():
    for role, uid in (("line_supervisor", "u_sup_l1"), ("plant_head", "u_head")):
        with role_conn(role, uid) as c:
            assert q(c, "SHOW transaction_read_only")[0][0] == "on"

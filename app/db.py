"""Database access.

Two kinds of connection, and the difference is the whole point:

  admin_conn()  - the owner login. Used for ingest, the graph mirror and the
                  users table. Never used to run model-generated SQL.
  role_conn()   - the restricted login for one application role, with
                  `SET LOCAL app.user_id` applied so RLS can derive scope.
                  Every model-generated SELECT runs here, and nowhere else.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg

from app.config import settings

# app role -> (postgres login role, schema holding that role's views)
ROLE_LOGIN = {
    "line_supervisor": ("app_line_supervisor", "sup"),
    "quality_engineer": ("app_quality_engineer", "qe"),
    "plant_head": ("app_plant_head", "head"),
}

# What the model is allowed to know exists, per role. Used to build the prompt
# schema and by the sqlglot guard as the allow-list.
ROLE_TABLES = {
    "line_supervisor": ["machines", "work_orders", "downtime_events"],
    "quality_engineer": ["machines", "work_orders", "downtime_events", "defects", "supplier_parts"],
    "plant_head": ["machines", "work_orders", "downtime_events", "defects", "supplier_parts"],
}

ROLE_COLUMNS = {
    "machines": ["machine_id", "line", "type", "model", "commissioned_on"],
    "work_orders": ["wo_id", "machine_id", "part_no", "qty_planned", "qty_good",
                    "started_at", "ended_at"],
    "downtime_events": ["event_id", "machine_id", "start_at", "end_at", "category",
                        "reason_code", "alarm_code"],
    "defects": ["defect_id", "wo_id", "part_no", "defect_type", "qty", "found_at", "stage"],
    # Per role, because the price column exists for the plant head only.
    "supplier_parts:quality_engineer": ["part_no", "supplier", "valid_from"],
    "supplier_parts:plant_head": ["part_no", "supplier", "unit_price_inr", "valid_from"],
}


def columns_for(role: str, table: str) -> list[str]:
    return ROLE_COLUMNS.get(f"{table}:{role}") or ROLE_COLUMNS.get(table, [])


def schema_for_role(role: str) -> str:
    """The only schema description the SQL model ever sees."""
    lines = []
    for t in ROLE_TABLES.get(role, []):
        cols = ", ".join(columns_for(role, t))
        lines.append(f"{t}({cols})")
    return "\n".join(lines)


@contextmanager
def admin_conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(settings.pg_dsn(), autocommit=False) as conn:
        yield conn


@contextmanager
def role_conn(role: str, user_id: str) -> Iterator[psycopg.Connection]:
    """A connection as the restricted role, scoped to one user for RLS.

    The transaction is read-only and `app.user_id` is set with SET LOCAL so it
    cannot outlive the request.
    """
    if role not in ROLE_LOGIN:
        raise ValueError(f"unknown app role: {role!r}")
    login, _schema = ROLE_LOGIN[role]
    dsn = settings.pg_dsn(user=login, password=settings.pg_role_password)
    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            # SET LOCAL takes no bind parameters, and interpolating a user id
            # into SQL is exactly what we must not do. set_config(..., true) is
            # the parameterised equivalent and is transaction-local.
            cur.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))
            cur.execute("SET LOCAL statement_timeout = '5s'")
        yield conn
        conn.rollback()  # nothing here is ever allowed to commit


def get_user(user_id: str) -> dict | None:
    """Role and scope come from here and from nowhere else."""
    with admin_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT user_id, name, role, scope FROM users WHERE user_id = %s", (user_id,)
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"user_id": row[0], "name": row[1], "role": row[2], "scope": row[3]}


def list_users() -> list[dict]:
    with admin_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id, name, role, scope FROM users ORDER BY user_id")
        rows = cur.fetchall()
    return [{"user_id": r[0], "name": r[1], "role": r[2], "scope": r[3]} for r in rows]

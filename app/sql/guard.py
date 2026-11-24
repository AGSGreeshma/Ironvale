"""Static validation of model-generated SQL, before it reaches the database.

This is a whitelist, not a blacklist. A statement is rejected unless it is:
  * exactly one statement
  * a SELECT (optionally with CTEs)
  * reading only the views belonging to the asking role
  * free of any function on the denylist

The database enforces the same boundary independently - the role has SELECT on
its own views and nothing else, sessions are read-only, and RLS restricts rows -
so this guard is the outer layer rather than the only one. It exists because a
clear rejection with `sql_rejected` is a better outcome than a permission error,
and because "only that role's views" is cheaper to prove here than in Postgres.

LIMIT 500 is appended when the model omits it.
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from app.db import ROLE_LOGIN, ROLE_TABLES

DIALECT = "postgres"
DEFAULT_LIMIT = 500

# Functions that could read outside the database, write, sleep or escalate.
DENY_FUNCTIONS = frozenset({
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export", "dblink", "dblink_exec", "pg_sleep",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
    "set_config", "current_setting", "pg_logical_emit_message",
    "query_to_xml", "pg_read_server_files", "copy", "pg_file_write",
})

# Statement types that are never acceptable, even inside a CTE.
FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.TruncateTable, exp.Grant, exp.Command, exp.Transaction, exp.Commit,
    exp.Rollback, exp.Set, exp.Use, exp.Merge, exp.Copy,
)


@dataclass
class GuardResult:
    ok: bool
    sql: str = ""
    reason: str | None = None
    tables: tuple[str, ...] = ()
    added_limit: bool = False

    @property
    def error_class(self) -> str | None:
        return None if self.ok else "sql_rejected"


def _allowed_tables(role: str) -> set[str]:
    return set(ROLE_TABLES.get(role, []))


def _allowed_schema(role: str) -> str:
    return ROLE_LOGIN[role][1] if role in ROLE_LOGIN else ""


def validate(sql: str, role: str, add_limit: bool = True) -> GuardResult:
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        return GuardResult(False, reason="empty statement")

    try:
        statements = sqlglot.parse(sql, dialect=DIALECT)
    except Exception as exc:
        return GuardResult(False, reason=f"unparseable SQL: {exc}")

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        return GuardResult(
            False, reason=f"expected exactly one statement, got {len(statements)}"
        )

    tree = statements[0]

    # Must be a SELECT at the top level. sqlglot attaches CTEs to the SELECT
    # itself, so `WITH x AS (...) SELECT ...` parses as a Select and is fine.
    if not isinstance(tree, (exp.Select, exp.Union)):
        return GuardResult(False, reason=f"only SELECT is allowed, got {type(tree).__name__}")

    for node_type in FORBIDDEN_NODES:
        if list(tree.find_all(node_type)):
            return GuardResult(
                False, reason=f"statement contains a forbidden {node_type.__name__} node"
            )

    # Into / writeable select targets.
    if list(tree.find_all(exp.Into)):
        return GuardResult(False, reason="SELECT ... INTO is not allowed")

    # CTE names are legitimate table references.
    cte_names = {
        c.alias_or_name.lower() for c in tree.find_all(exp.CTE) if c.alias_or_name
    }

    allowed = _allowed_tables(role)
    role_schema = _allowed_schema(role)
    seen: list[str] = []

    for tbl in tree.find_all(exp.Table):
        name = (tbl.name or "").lower()
        schema = (tbl.db or "").lower()
        if not name:
            continue
        if name in cte_names and not schema:
            continue
        if schema and schema != role_schema:
            return GuardResult(
                False,
                reason=f"table {schema}.{name} is outside this role's schema "
                       f"({role_schema})",
            )
        if name not in allowed:
            return GuardResult(
                False,
                reason=f"table {name!r} is not readable by this role "
                       f"(allowed: {', '.join(sorted(allowed))})",
            )
        seen.append(name)

    if not seen:
        return GuardResult(False, reason="no readable table referenced")

    # Unknown/bare functions parse as Anonymous; known ones get their own node
    # type, so check both the parsed names and the raw text.
    for fn in tree.find_all(exp.Anonymous):
        fname = fn.this if isinstance(fn.this, str) else ""
        if fname.lower() in DENY_FUNCTIONS:
            return GuardResult(False, reason=f"function {fname}() is not allowed")

    lowered = sql.lower()
    for bad in DENY_FUNCTIONS:
        if f"{bad}(" in lowered.replace(" ", ""):
            return GuardResult(False, reason=f"function {bad}() is not allowed")

    added = False
    if add_limit and isinstance(tree, exp.Select) and not tree.args.get("limit"):
        tree = tree.limit(DEFAULT_LIMIT)
        added = True
    elif add_limit and isinstance(tree, exp.Union) and not tree.args.get("limit"):
        tree = exp.select("*").from_(tree.subquery("u")).limit(DEFAULT_LIMIT)
        added = True

    return GuardResult(
        ok=True,
        sql=tree.sql(dialect=DIALECT),
        tables=tuple(sorted(set(seen))),
        added_limit=added,
    )

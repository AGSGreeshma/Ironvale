"""The data route: question -> SQL -> rows -> answer.

Generation is a single prompt carrying only the asking role's view schema plus
few-shot examples (rather than Vanna, which would add a vector store and a
training step for a five-table schema; noted in the README under Decisions).

The metric definitions are fixed in the prompt and restated in `metric_note`
whenever a question uses a term we had to define, because "downtime" and
"defect rate" both have several defensible readings and the answer should say
which one it used.

Row scope matters for the status. Zero rows because the asker's line is not the
line they asked about is `not_found` - the honest answer is "not something you
can see", not an empty chart. Zero rows inside their own scope is `answered`
with "No matching records."
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from decimal import Decimal

import psycopg

from app.access import Principal
from app.config import settings
from app.db import ROLE_LOGIN, role_conn, schema_for_role
from app.llm import generate
from app.models import Chart, SqlResult
from app.sql.chart import pick_chart
from app.sql.guard import validate

PROMPT_VERSION = "sql-v1.3.0"

MAX_RETURNED_ROWS = 50

METRIC_RULES = """Metric definitions - use these exactly:
- downtime minutes = EXTRACT(EPOCH FROM (end_at - start_at)) / 60
- breakdown downtime = downtime minutes WHERE category = 'breakdown' only
- yield = SUM(qty_good)::numeric / NULLIF(SUM(qty_planned), 0)
- defect rate = SUM(defect qty)::numeric / NULLIF(SUM(qty_good), 0) for the
  same part and the same period
- changeover time = downtime minutes WHERE category = 'changeover'
- category is one of: planned, breakdown, changeover, material"""

FEWSHOT = """Examples.

Q: Breakdown downtime by machine on Line 1 in October 2026
SQL: SELECT m.machine_id, SUM(EXTRACT(EPOCH FROM (d.end_at - d.start_at)) / 60) AS downtime_minutes
FROM downtime_events d
JOIN machines m ON m.machine_id = d.machine_id
WHERE m.line = 'Line 1' AND d.category = 'breakdown'
  AND d.start_at >= '2026-10-01' AND d.start_at < '2026-11-01'
GROUP BY m.machine_id
ORDER BY m.machine_id

Q: Total downtime by category in September 2026
SQL: SELECT d.category, SUM(EXTRACT(EPOCH FROM (d.end_at - d.start_at)) / 60) AS downtime_minutes
FROM downtime_events d
WHERE d.start_at >= '2026-09-01' AND d.start_at < '2026-10-01'
GROUP BY d.category
ORDER BY downtime_minutes DESC

Q: What is the yield on Line 3 in October 2026?
SQL: SELECT SUM(w.qty_good)::numeric / NULLIF(SUM(w.qty_planned), 0) AS yield
FROM work_orders w
JOIN machines m ON m.machine_id = w.machine_id
WHERE m.line = 'Line 3'
  AND w.started_at >= '2026-10-01' AND w.started_at < '2026-11-01'

Q: Average changeover time by machine on Line 1
SQL: SELECT m.machine_id, AVG(EXTRACT(EPOCH FROM (d.end_at - d.start_at)) / 60) AS avg_changeover_minutes
FROM downtime_events d
JOIN machines m ON m.machine_id = d.machine_id
WHERE m.line = 'Line 1' AND d.category = 'changeover'
GROUP BY m.machine_id
ORDER BY m.machine_id

Q: How many downtime events carried alarm code E-417?
SQL: SELECT count(*) AS events
FROM downtime_events
WHERE alarm_code = 'E-417'

Q: Downtime minutes per day in October 2026
SQL: SELECT d.start_at::date AS day, SUM(EXTRACT(EPOCH FROM (d.end_at - d.start_at)) / 60) AS downtime_minutes
FROM downtime_events d
WHERE d.start_at >= '2026-10-01' AND d.start_at < '2026-11-01'
GROUP BY day
ORDER BY day

Q: Defect rate for HX-220-B in September 2026
SQL: SELECT SUM(df.qty)::numeric / NULLIF(SUM(w.qty_good), 0) AS defect_rate
FROM defects df
JOIN work_orders w ON w.wo_id = df.wo_id
WHERE df.part_no = 'HX-220-B'
  AND df.found_at >= '2026-09-01' AND df.found_at < '2026-10-01'

Q: Which defect types were most common on Line 1 in September 2026?
SQL: SELECT df.defect_type, SUM(df.qty) AS qty
FROM defects df
JOIN work_orders w ON w.wo_id = df.wo_id
JOIN machines m ON m.machine_id = w.machine_id
WHERE m.line = 'Line 1'
  AND df.found_at >= '2026-09-01' AND df.found_at < '2026-10-01'
GROUP BY df.defect_type
ORDER BY qty DESC

Q: Top 5 machines by breakdown count in August 2026
SQL: SELECT machine_id, count(*) AS breakdowns
FROM downtime_events
WHERE category = 'breakdown'
  AND start_at >= '2026-08-01' AND start_at < '2026-09-01'
GROUP BY machine_id
ORDER BY breakdowns DESC
LIMIT 5

Q: How many work orders ran on CNC-L1-04 in September 2026?
SQL: SELECT count(*) AS work_orders
FROM work_orders
WHERE machine_id = 'CNC-L1-04'
  AND started_at >= '2026-09-01' AND started_at < '2026-10-01'

Q: Which suppliers supply HX-220-B?
SQL: SELECT DISTINCT supplier
FROM supplier_parts
WHERE part_no = 'HX-220-B'

Q: Monthly breakdown downtime trend
SQL: SELECT to_char(date_trunc('month', start_at), 'YYYY-MM') AS month,
       SUM(EXTRACT(EPOCH FROM (end_at - start_at)) / 60) AS downtime_minutes
FROM downtime_events
WHERE category = 'breakdown'
GROUP BY month
ORDER BY month"""


def system_prompt(role: str) -> str:
    return f"""You translate questions about the Ironvale Components plant into a \
single PostgreSQL SELECT statement.

You may read ONLY these views. This is the entire schema that exists for you:

{schema_for_role(role)}

Rules:
- Output ONE SELECT statement and nothing else. No prose, no code fence, no
  semicolon, no explanation.
- Never write INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, TRUNCATE or GRANT.
- Never reference a table that is not listed above. Never use a schema prefix.
- Use unqualified view names exactly as listed.
- Timestamps are timestamptz. Use half-open date ranges: >= start AND < end.
- Label computed columns with a clear alias.
- When a question asks "by machine" or "by category", GROUP BY that column and
  return exactly two columns: the grouping column and the number.
- Do not add a LIMIT unless the question asks for a top-N.

{METRIC_RULES}

{FEWSHOT}"""


# Metric words that need their definition restating in the answer.
AMBIGUOUS_METRICS = {
    "downtime": "downtime minutes = end_at - start_at, summed",
    "breakdown downtime": "breakdown downtime = category 'breakdown' only",
    "yield": "yield = qty_good / qty_planned",
    "defect rate": "defect rate = defect qty / qty_good for the same part and period",
    "oee": "OEE is not defined in this dataset",
    "availability": "availability is not defined in this dataset; "
                    "downtime minutes were used instead",
    "mtbf": "MTBF is not defined in this dataset",
    "mttr": "MTTR is not defined in this dataset; "
            "mean breakdown duration in minutes was used instead",
    "utilisation": "utilisation is not defined in this dataset",
    "utilization": "utilisation is not defined in this dataset",
    "efficiency": "efficiency is not defined in this dataset; yield was used instead",
    "scrap rate": "scrap rate is not defined separately; defect rate was used instead",
}

UNDEFINED_METRICS = {"oee", "availability", "mtbf", "mttr", "utilisation",
                     "utilization", "efficiency", "scrap rate"}

# Columns that would put a named individual in front of the model.
PERSON_LEVEL_COLUMNS = {"name", "user_id", "operator", "operator_name",
                        "prepared_by", "approved_by", "employee", "emp_no"}

_LINE_IN_TEXT = re.compile(r"\bline\s*([123])\b", re.I)
_MACHINE_IN_TEXT = re.compile(r"\b(CNC-L1|FUR-L2|PRS-L3|LKT-L3)-\d{2}\b", re.I)


@dataclass
class DataAnswer:
    status: str                      # answered | not_found | error
    answer: str
    sql_result: SqlResult | None = None
    error_class: str | None = None
    metric_note: str | None = None
    guards: list[str] = field(default_factory=list)
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_inr: float = 0.0
    cached: bool = False
    attempts: int = 1
    generated_sql: str = ""
    rejected_reason: str | None = None
    repaired: bool = False
    timings_ms: dict = field(default_factory=dict)


def referenced_lines(text: str) -> set[str]:
    out = {f"Line {m}" for m in _LINE_IN_TEXT.findall(text or "")}
    for m in _MACHINE_IN_TEXT.findall(text or ""):
        out.add(f"Line {m.upper().split('-')[1][1]}")
    return out


def scope_conflict(text: str, principal: Principal) -> str | None:
    """Is the question about a line this user cannot see?

    Returns the offending line, or None. This is what makes a Line 3 question
    from a Line 1 supervisor `not_found` instead of an empty chart.
    """
    if principal.role != "line_supervisor":
        return None
    asked = referenced_lines(text)
    if not asked:
        return None
    outside = sorted(asked - {principal.scope})
    return outside[0] if outside else None


def metric_note_for(text: str) -> str | None:
    t = (text or "").lower()
    notes = []
    for term in sorted(AMBIGUOUS_METRICS, key=len, reverse=True):
        if term in t:
            notes.append(AMBIGUOUS_METRICS[term])
            if term in UNDEFINED_METRICS:
                break
    if not notes:
        return None
    return "Definition used: " + "; ".join(dict.fromkeys(notes)) + "."


def _coerce(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    return v


def run_sql(sql: str, principal: Principal):
    """Execute as the restricted role. Returns (columns, rows) or raises."""
    with role_conn(principal.role, principal.user_id) as conn, conn.cursor() as cur:
        cur.execute(sql)
        columns = [d.name for d in (cur.description or [])]
        rows = cur.fetchall()
    return columns, [[_coerce(v) for v in r] for r in rows]


def _describe(columns, rows, chart: Chart, metric_note: str | None) -> str:
    """A template answer built from the result, not written by the model.

    Aggregates only. No row ever goes back to an LLM to be narrated, which is
    how the person-level rule is satisfied by construction.
    """
    n = len(rows)
    if n == 0:
        return "No matching records."

    if n == 1 and len(columns) == 1:
        val = rows[0][0]
        if isinstance(val, float):
            val = round(val, 4)
        body = f"{columns[0].replace('_', ' ')}: {val}."
    elif chart.type in ("bar", "line") and chart.x and chart.y:
        top = sorted(
            [r for r in rows if isinstance(r[1], (int, float))],
            key=lambda r: -float(r[1]),
        )[:3]
        total = sum(float(r[1]) for r in rows if isinstance(r[1], (int, float)))
        parts = ", ".join(
            f"{r[0]} {round(float(r[1]), 1)}" for r in top
        )
        body = (
            f"{n} {chart.x.replace('_', ' ')} values returned, "
            f"total {chart.y.replace('_', ' ')} {round(total, 1)}. "
            f"Highest: {parts}."
        )
    else:
        body = f"{n} row{'s' if n != 1 else ''} returned."

    if metric_note:
        body = f"{body} {metric_note}"
    return body


def answer_data(question: str, principal: Principal, trace=None) -> DataAnswer:
    """Full data route."""
    guards: list[str] = []

    # 1. Row scope, before any model call. A question about someone else's line
    #    is not a query to be run and then emptied.
    bad_line = scope_conflict(question, principal)
    if bad_line:
        guards.append(f"row_scope_denied:{bad_line}")
        return DataAnswer(
            status="not_found", answer=settings.MSG_NOT_FOUND, guards=guards,
        )

    note = metric_note_for(question)

    # 2. Generate
    try:
        res = generate(
            prompt=f"Q: {question}\nSQL:",
            system=system_prompt(principal.role),
            model=settings.answer_model,
            max_tokens=320,
        )
    except Exception as exc:
        return DataAnswer(
            status="error", answer=settings.MSG_ERROR, error_class="llm_unavailable",
            guards=guards + [str(exc)[:80]],
        )

    raw_sql = _clean_sql(res.text)
    totals = {"p": res.prompt_tokens, "c": res.completion_tokens, "cost": res.cost_inr}

    # 3. Validate
    g = validate(raw_sql, principal.role)
    if not g.ok:
        return DataAnswer(
            status="error", answer=settings.MSG_ERROR, error_class="sql_rejected",
            generated_sql=raw_sql, rejected_reason=g.reason, guards=guards,
            model=res.model, prompt_tokens=totals["p"],
            completion_tokens=totals["c"], cost_inr=totals["cost"],
            cached=res.cached,
        )

    sql = g.sql
    repaired = False
    attempts = 1

    # 4. Execute, with one repair attempt using the database's own error text.
    try:
        columns, rows = run_sql(sql, principal)
    except psycopg.Error as exc:
        db_error = str(getattr(exc, "diag", None) and exc.diag.message_primary or exc)
        attempts = 2
        try:
            res2 = generate(
                prompt=(
                    f"Q: {question}\n"
                    f"Your previous SQL failed.\n"
                    f"SQL: {sql}\n"
                    f"PostgreSQL error: {db_error}\n"
                    f"Write a corrected single SELECT statement.\nSQL:"
                ),
                system=system_prompt(principal.role),
                model=settings.answer_model,
                max_tokens=320,
            )
            totals["p"] += res2.prompt_tokens
            totals["c"] += res2.completion_tokens
            totals["cost"] += res2.cost_inr
            g2 = validate(_clean_sql(res2.text), principal.role)
            if not g2.ok:
                return DataAnswer(
                    status="error", answer=settings.MSG_ERROR,
                    error_class="sql_rejected", generated_sql=_clean_sql(res2.text),
                    rejected_reason=g2.reason, guards=guards + ["repair_rejected"],
                    model=res.model, prompt_tokens=totals["p"],
                    completion_tokens=totals["c"], cost_inr=totals["cost"],
                    attempts=2,
                )
            sql = g2.sql
            repaired = True
            columns, rows = run_sql(sql, principal)
        except psycopg.Error:
            return DataAnswer(
                status="error", answer=settings.MSG_ERROR,
                error_class="sql_execution_failed", generated_sql=sql,
                guards=guards + ["repair_failed"], model=res.model,
                prompt_tokens=totals["p"], completion_tokens=totals["c"],
                cost_inr=totals["cost"], attempts=2, repaired=True,
            )
        except Exception as exc:
            return DataAnswer(
                status="error", answer=settings.MSG_ERROR,
                error_class="llm_unavailable", guards=guards + [str(exc)[:80]],
                attempts=2,
            )

    # 5. Person-level guard: never narrate a row that names an individual.
    lowered = {c.lower() for c in columns}
    person_level = bool(lowered & PERSON_LEVEL_COLUMNS)
    if person_level:
        guards.append("person_level_template_answer")

    chart = pick_chart(columns, rows)
    row_count = len(rows)
    shown = rows[:MAX_RETURNED_ROWS]

    # 6. Zero rows: inside scope is an answer, outside scope is not_found.
    if row_count == 0:
        bad = scope_conflict(question, principal)
        if bad:
            return DataAnswer(
                status="not_found", answer=settings.MSG_NOT_FOUND,
                guards=guards + [f"row_scope_denied:{bad}"],
            )
        sql_result = SqlResult(
            sql=sql, columns=columns, rows=[], row_count=0,
            chart=Chart(type="table"), metric_note=note,
        )
        return DataAnswer(
            status="answered", answer="No matching records.",
            sql_result=sql_result, metric_note=note, guards=guards,
            model=res.model, prompt_tokens=totals["p"],
            completion_tokens=totals["c"], cost_inr=totals["cost"],
            cached=res.cached, attempts=attempts, generated_sql=raw_sql,
            repaired=repaired,
        )

    sql_result = SqlResult(
        sql=sql, columns=columns, rows=shown, row_count=row_count,
        chart=chart, metric_note=note,
    )
    return DataAnswer(
        status="answered",
        answer=_describe(columns, rows, chart, note),
        sql_result=sql_result, metric_note=note, guards=guards,
        model=res.model, prompt_tokens=totals["p"],
        completion_tokens=totals["c"], cost_inr=totals["cost"],
        cached=res.cached, attempts=attempts, generated_sql=raw_sql,
        repaired=repaired,
    )


_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.S | re.I)


def _clean_sql(text: str) -> str:
    """Pull the statement out of whatever the model wrapped it in."""
    t = (text or "").strip()
    m = _FENCE.search(t)
    if m:
        t = m.group(1).strip()
    t = re.sub(r"^\s*(?:SQL|Query|Answer)\s*:\s*", "", t, flags=re.I)
    # Drop any trailing prose after the statement.
    if ";" in t:
        t = t.split(";")[0]
    return t.strip()

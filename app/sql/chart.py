"""Chart selection from the result shape.

The rule, exactly as specified:
    one category column + one number  -> bar
    a date column + one number        -> line
    anything else                     -> table only

Deciding from the returned data rather than asking the model keeps it
deterministic and testable.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

from app.models import Chart

NUMERIC = (int, float, Decimal)
TEMPORAL = (datetime.date, datetime.datetime)


def _column_kind(values: list) -> str:
    """'number' | 'date' | 'category' | 'empty', from the non-null values."""
    vals = [v for v in values if v is not None]
    if not vals:
        return "empty"
    if all(isinstance(v, bool) for v in vals):
        return "category"
    if all(isinstance(v, NUMERIC) and not isinstance(v, bool) for v in vals):
        return "number"
    if all(isinstance(v, TEMPORAL) for v in vals):
        return "date"
    # A text column holding only date-like strings counts as temporal: SQL that
    # buckets by month with to_char() returns text.
    if all(isinstance(v, str) for v in vals):
        if all(_looks_like_date(v) for v in vals):
            return "date"
        return "category"
    return "category"


def _looks_like_date(s: str) -> bool:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y/%m/%d", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            datetime.datetime.strptime(s, fmt)
            return True
        except ValueError:
            continue
    return False


def pick_chart(columns: list[str], rows: list[list]) -> Chart:
    if not columns or not rows:
        return Chart(type="table")
    if len(columns) != 2:
        return Chart(type="table")

    kinds = [
        _column_kind([r[i] if i < len(r) else None for r in rows])
        for i in range(2)
    ]

    # Exactly one number, paired with one date or one category.
    number_idx = [i for i, k in enumerate(kinds) if k == "number"]
    if len(number_idx) != 1:
        return Chart(type="table")

    y = number_idx[0]
    x = 1 - y
    if kinds[x] == "date":
        return Chart(type="line", x=columns[x], y=columns[y])
    if kinds[x] == "category":
        return Chart(type="bar", x=columns[x], y=columns[y])
    return Chart(type="table")

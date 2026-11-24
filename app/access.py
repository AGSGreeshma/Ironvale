"""Who may see what.

Role and scope are read from the users table and from nowhere else. Anything a
question *claims* about the asker ("I'm the plant head") is ignored: it never
reaches this module, which only ever takes a user_id.

Two different kinds of scope, and conflating them is a bug:

  document scope  - metadata on a chunk (L1/L2/L3/plant). It is NOT a per-user
                    gate. A Line 3 supervisor is expected to be able to read
                    the Line 2 furnace SOP (golden question G03), so procedures
                    and manuals are readable by every role and document access
                    is decided by allowed_roles alone. Document scope is used
                    as a query-time filter when the question itself is about a
                    particular line, and that filter is applied inside both
                    retrieval queries.

  data row scope  - enforced in Postgres by RLS. This is where a supervisor is
                    confined to their own line, which is why a Line 1
                    supervisor asking about Line 3 downtime gets not_found.
"""
from __future__ import annotations

import re

from app.db import get_user

ROLES = ("line_supervisor", "quality_engineer", "plant_head")

# Document scopes that exist in the corpus.
SCOPES = ("L1", "L2", "L3", "plant")

_LINE_WORD = {
    "L1": re.compile(r"\b(line\s*1|l1|cnc[- ]?l1|dmc\s*850)\b", re.I),
    "L2": re.compile(r"\b(line\s*2|l2|fur[- ]?l2|furnace|heat\s*treat\w*|quench|soak)\b", re.I),
    "L3": re.compile(r"\b(line\s*3|l3|prs[- ]?l3|lkt[- ]?l3|assembly|leak\s*test|press[- ]?fit)\b", re.I),
}

_MACHINE_LINE = re.compile(r"\b(CNC-L1-\d{2}|FUR-L2-\d{2}|PRS-L3-\d{2}|LKT-L3-\d{2})\b", re.I)


class Principal:
    """The resolved identity a request runs as."""

    __slots__ = ("user_id", "name", "role", "scope")

    def __init__(self, user_id: str, name: str, role: str, scope: str):
        self.user_id = user_id
        self.name = name
        self.role = role
        self.scope = scope

    @property
    def line(self) -> str | None:
        """'Line 1' -> 'L1'. None for QE and the plant head (all lines)."""
        m = re.match(r"line\s*(\d)", self.scope.strip(), re.I)
        return f"L{m.group(1)}" if m else None

    def can_open(self, allowed_roles) -> bool:
        return self.role in set(allowed_roles or ())

    def __repr__(self) -> str:  # pragma: no cover
        return f"Principal({self.user_id}, {self.role}, {self.scope})"


def resolve(user_id: str) -> Principal | None:
    """Server-side identity lookup. Returns None for an unknown user_id."""
    rec = get_user(user_id)
    if not rec:
        return None
    return Principal(rec["user_id"], rec["name"], rec["role"], rec["scope"])


def scope_filter_for_question(text: str) -> list[str] | None:
    """Document scopes worth searching for this question, or None for all.

    Derived from the question, never from the asker: it is a relevance filter,
    not an access filter. 'plant' is always included because plant-wide
    procedures apply to every line.
    """
    if not text:
        return None
    hits = set()
    for mid in _MACHINE_LINE.findall(text):
        hits.add(f"L{mid.split('-')[1][1]}")
    for scope, rx in _LINE_WORD.items():
        if rx.search(text):
            hits.add(scope)
    if not hits or len(hits) == len(_LINE_WORD):
        return None
    return sorted(hits | {"plant"})


def visible_doc_ids(principal: Principal, doc_ids) -> list[str]:
    """Filter a set of doc ids down to the ones this principal may open.

    Used by the graph route, which must drop a path whose supporting documents
    the user cannot open.
    """
    from app.retrieval.store import doc_access_map

    amap = doc_access_map()
    return [d for d in doc_ids if principal.role in amap.get(d, ())]

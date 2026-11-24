"""Entity resolution.

"Arcflow Castings Pvt. Ltd.", "ARCFLOW CASTINGS" and "Arcflow castings pvt ltd"
are one supplier. Without normalisation the graph grows three Supplier nodes and
`supplier_exposure('Arcflow Castings')` silently misses two thirds of the
exposure.

Normalisation: casefold, strip punctuation, drop corporate suffixes, collapse
whitespace. Then a canonical display name is chosen as the longest original
spelling seen for that key, so the answer reads naturally.
"""
from __future__ import annotations

import re

LABELS = ("Machine", "Part", "Supplier", "Deviation", "SOP")

RELATIONSHIPS = {
    "RUNS": ("Machine", "Part"),
    "SUPPLIED_BY": ("Part", "Supplier"),
    "FOUND_ON": ("Deviation", "Machine"),
    "INVOLVES": ("Deviation", "Part"),
    "COVERS": ("SOP", "Machine"),
}

# Corporate suffixes stripped during normalisation.
SUFFIXES = [
    "private limited", "pvt limited", "pvt ltd", "pvt. ltd.", "private ltd",
    "limited", "ltd", "llp", "inc", "incorporated", "corp", "corporation",
    "co", "company", "gmbh", "plc", "sa", "ag", "bv", "pte",
]

_PUNCT = re.compile(r"[^\w\s\-]")
_WS = re.compile(r"\s+")


def normalise(name: str) -> str:
    """Canonical lookup key for an entity name."""
    if not name:
        return ""
    s = str(name).strip().casefold()
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip()

    # Strip suffixes repeatedly: "Foo Pvt Ltd Limited" -> "foo"
    changed = True
    while changed:
        changed = False
        for suf in SUFFIXES:
            if s.endswith(" " + suf):
                s = s[: -(len(suf) + 1)].strip()
                changed = True
    # Identifiers keep their hyphens; prose names lose stray ones.
    return _WS.sub(" ", s).strip()


def node_key(label: str, name: str) -> str:
    """Stable primary key for a node: 'Label:normalised name'."""
    if label not in LABELS:
        raise ValueError(f"unknown label {label!r}")
    return f"{label}:{normalise(name)}"


def display_key(label: str, name: str) -> str:
    """The form used in graph.paths, with the canonical display name."""
    return f"{label}:{name}"


class Resolver:
    """Collects spellings and returns one canonical name per entity."""

    def __init__(self):
        self._seen: dict[str, dict[str, int]] = {}

    def add(self, label: str, name: str) -> str:
        key = node_key(label, name)
        bucket = self._seen.setdefault(key, {})
        clean = _WS.sub(" ", str(name).strip())
        bucket[clean] = bucket.get(clean, 0) + 1
        return key

    def canonical(self, label: str, name: str) -> str:
        key = node_key(label, name)
        bucket = self._seen.get(key)
        if not bucket:
            return _WS.sub(" ", str(name).strip())
        # Most frequent spelling wins; ties go to the longest (most specific).
        return sorted(bucket.items(), key=lambda kv: (-kv[1], -len(kv[0])))[0][0]

    def keys(self) -> list[str]:
        return sorted(self._seen)


# Machine id expansion, used when an SOP says which machines it applies to.
_MACHINE_RE = re.compile(r"\b((?:CNC|FUR|PRS|LKT)-L\d)-(\d{2})\b", re.I)
_RANGE_RE = re.compile(
    r"\b((?:CNC|FUR|PRS|LKT)-L\d)-(\d{2})\s*(?:through|to|-|–)\s*"
    r"(?:(?:CNC|FUR|PRS|LKT)-L\d-)?(\d{2})\b",
    re.I,
)


def expand_machines(text: str, known: set[str] | None = None) -> list[str]:
    """Machine ids named in a free-text 'Applies to' field.

    Handles "CNC-L1-01 through CNC-L1-06" as well as comma-separated ids.
    """
    out: set[str] = set()

    for prefix, lo, hi in _RANGE_RE.findall(text or ""):
        p = prefix.upper()
        a, b = int(lo), int(hi)
        if a <= b and b - a < 50:
            for i in range(a, b + 1):
                out.add(f"{p}-{i:02d}")

    for prefix, num in _MACHINE_RE.findall(text or ""):
        out.add(f"{prefix.upper()}-{num}")

    if known is not None:
        out &= known
    return sorted(out)


_LINE_RE = re.compile(r"\bline\s*([123])\b", re.I)


def line_of_machine(machine_id: str) -> str | None:
    m = re.match(r"^(?:CNC|FUR|PRS|LKT)-L(\d)-\d{2}$", machine_id or "", re.I)
    return f"Line {m.group(1)}" if m else None


def parse_line(text: str) -> str | None:
    m = _LINE_RE.search(text or "")
    return f"Line {m.group(1)}" if m else None

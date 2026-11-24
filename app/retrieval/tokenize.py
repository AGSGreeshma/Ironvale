"""Tokenizer for the sparse (BM25) index.

The whole point of this file is that `E-417` must survive as one token. A
default word tokenizer splits it into `e` and `417`, at which point E-417 and
E-471 both become the bag {e, 4, 1, 7} and the retriever can no longer tell
them apart - which is the single failure the brief calls out hardest.

So: identifier-shaped tokens are lifted out whole first, canonicalised, and
emitted alongside a compact variant so a user typing `E417` still matches.
Only then is the remaining prose tokenized normally.
"""
from __future__ import annotations

import re

# ---- identifier patterns, matched in this order -------------------------
# Order matters: part numbers (HX-220-B) must be tried before alarm codes so
# the leading 'HX-220' is not grabbed as a code.
PATTERNS: list[tuple[str, re.Pattern]] = [
    # SOP-L2-011, WI-L3-009, MAN-CNC-04, QDR-2026-014, ASL-2026, SQA-ARC-01, PRC-2026-Q3
    ("docid", re.compile(r"\b(?:SOP|WI|MAN|QDR|ASL|SQA|PRC)-[A-Z0-9]+(?:-[A-Z0-9]+)*\b", re.I)),
    # CNC-L1-04, FUR-L2-01, PRS-L3-01, LKT-L3-01
    ("machine", re.compile(r"\b(?:CNC|FUR|PRS|LKT)-L\d-\d{2}\b", re.I)),
    # HX-220-B, BK-310-C, SH-090-D, SL-220-R, FS-M8-FL, BR-6208
    ("part", re.compile(r"\b[A-Z]{2}-[A-Z0-9]{2,4}(?:-[A-Z0-9]{1,3})?\b")),
    # E-417, E-471, F-210, L-110, W-233, M-145  (also E417 / E 417 / E_417)
    ("code", re.compile(r"\b([EWMFLP])[-–—_. ]?(\d{3})\b", re.I)),
    # M8, M10, M12 fastener sizes
    ("fastener", re.compile(r"\bM(\d{1,2})\b")),
    # 24 Nm, 45 minutes, 860 °C, 4 bar, 2.5 bar  -> value+unit kept together
    ("measure", re.compile(
        r"\b(\d+(?:\.\d+)?)\s*(Nm|nm|bar|minutes|minute|min|mins|mbar/min|mbar|"
        r"kN|kn|mm|kg|%|°C|degC|C)\b"
    )),
    ("version", re.compile(r"\bv\d+(?:\.\d+)*\b", re.I)),
    ("date", re.compile(r"\b\d{4}-\d{2}-\d{2}\b")),
]

_WORD = re.compile(r"[a-z0-9]+")

# Tokens that carry no retrieval signal in this corpus.
STOPWORDS = frozenset("""
a an and are as at be been but by do does for from had has have how i if in
into is it its of on or that the their then there these this to was were what
when where which who will with your you
""".split())


def canonical_code(letter: str, digits: str) -> str:
    return f"{letter.lower()}-{digits}"


def _emit_code(letter: str, digits: str) -> list[str]:
    """E-417 -> ['e-417', 'e417'] so both spellings and the typo form match."""
    return [canonical_code(letter, digits), f"{letter.lower()}{digits}"]


def exact_tokens(text: str) -> list[str]:
    """Just the identifier-shaped tokens, canonicalised."""
    out: list[str] = []
    for kind, rx in PATTERNS:
        for m in rx.finditer(text):
            if kind == "code":
                out.extend(_emit_code(m.group(1), m.group(2)))
            elif kind == "measure":
                val, unit = m.group(1), m.group(2)
                unit_n = {"degC": "c", "°C": "c", "C": "c", "min": "minutes",
                          "mins": "minutes", "minute": "minutes", "nm": "nm",
                          "kn": "kn"}.get(unit, unit.lower())
                out.append(f"{val}{unit_n}")
                out.append(unit_n)
            elif kind == "fastener":
                out.append(f"m{m.group(1)}")
            else:
                out.append(m.group(0).lower())
    return out


def tokenize(text: str) -> list[str]:
    """Full tokenization: identifiers kept whole, then ordinary words."""
    if not text:
        return []

    tokens: list[str] = []
    # Blank out identifier spans as we consume them so prose tokenization
    # cannot split them a second time.
    masked = text
    spans: list[tuple[int, int]] = []

    for kind, rx in PATTERNS:
        for m in rx.finditer(masked):
            if any(s <= m.start() < e or s < m.end() <= e for s, e in spans):
                continue
            if kind == "code":
                tokens.extend(_emit_code(m.group(1), m.group(2)))
            elif kind == "measure":
                val, unit = m.group(1), m.group(2)
                unit_n = {"degC": "c", "°C": "c", "C": "c", "min": "minutes",
                          "mins": "minutes", "minute": "minutes"}.get(unit, unit.lower())
                tokens.append(f"{val}{unit_n}")
                tokens.append(unit_n)
                tokens.append(val)
            elif kind == "fastener":
                tokens.append(f"m{m.group(1)}")
            else:
                tok = m.group(0).lower()
                tokens.append(tok)
                # Also index the parts of a compound id so "L2-011" or "2026-014"
                # retrieves its document.
                for piece in tok.split("-"):
                    if len(piece) > 1:
                        tokens.append(piece)
            spans.append((m.start(), m.end()))

    # Replace consumed spans with spaces, then tokenize what is left.
    if spans:
        chars = list(masked)
        for s, e in spans:
            for i in range(s, e):
                chars[i] = " "
        masked = "".join(chars)

    for w in _WORD.findall(masked.lower()):
        # Single letters carry no signal, but a single digit does: it is what
        # separates "Line 3" from "Line 1".
        if w in STOPWORDS or (len(w) == 1 and not w.isdigit()):
            continue
        tokens.append(w)

    return tokens

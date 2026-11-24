"""PII masking with Presidio, plus the guardrails that keep it from eating the
things the assistant exists to answer about.

Presidio on its own is dangerous here. 'Arcflow Castings' looks like a PERSON
to an NER model, 'HX-220-B' looks like an ID, and masking either one would
destroy a planted fact. So detection is followed by a subtraction step: any
span that overlaps the protected vocabulary is discarded before replacement.

What is masked:     person names, phone numbers, emails, ID/badge numbers
What is protected:  alarm and fault codes, part numbers, document ids, machine
                    ids, supplier/company names, versions, dates, measurements

The placeholder map is returned separately and is never written to the index,
never logged and never sent to a trace.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

# ---- protected vocabulary -------------------------------------------------
PROTECT_PATTERNS = [
    re.compile(r"\b(?:SOP|WI|MAN|QDR|ASL|SQA|PRC)-[A-Z0-9]+(?:-[A-Z0-9]+)*\b", re.I),
    re.compile(r"\b(?:CNC|FUR|PRS|LKT)-L\d-\d{2}\b", re.I),
    re.compile(r"\b[A-Z]{2}-[A-Z0-9]{2,4}(?:-[A-Z0-9]{1,3})?\b"),
    re.compile(r"\b[EWMFLP][-–—_. ]?\d{3}\b", re.I),
    re.compile(r"\bM\d{1,2}\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\bv\d+(?:\.\d+)*\b", re.I),
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:Nm|bar|mbar|minutes?|mins?|kN|mm|kg|%|°C)\b", re.I),
    re.compile(r"\bDMC\s*850\b|\bBF-1200\b|\bHP-250\b|\bLT-400\b", re.I),
    re.compile(r"\bRs\s*\d+\b", re.I),
    re.compile(r"\b8D\b"),
]

# Company names stay. Listed explicitly because NER reliably mis-tags them.
PROTECT_LITERALS = [
    "Arcflow Castings", "Arcflow", "Metsun Fasteners", "Metsun",
    "Tamron Seals", "Tamron", "Shakti Forgings", "Shakti",
    "Velkon Bearings", "Velkon", "Ironvale Components", "Ironvale",
    "Hosur", "Pvt. Ltd.", "Pvt Ltd", "Limited",
    # Section/role words a PERSON recognizer sometimes claims. OCR'd tables are
    # the worst offenders: 'Maintenance Yes' in a reset-authority column reads
    # as a two-word name to NER.
    "Quality Engineering", "Manufacturing Engineering", "Heat Treatment",
    "Line 1", "Line 2", "Line 3", "Brix", "Otsu",
    "Maintenance", "Operator", "Reset", "Spindle", "Coolant", "Interlock",
    "Axis", "Probe", "Hydraulics", "Changeover", "Quench", "Furnace",
    "Soak", "Torque", "Swarf", "Fixture", "Deburr", "Press", "Leak Test",
]

MASK_ENTITIES = [
    "PERSON",
    "PHONE_NUMBER",
    "EMAIL_ADDRESS",
    "IN_AADHAAR",
    "IN_PAN",
    "US_SSN",
    "CREDIT_CARD",
    "IRONVALE_BADGE",     # custom, below
]


@dataclass
class _Detection:
    """A detection we synthesised ourselves, shaped like a Presidio result."""

    start: int
    end: int
    entity_type: str
    score: float = 0.85


# Signature / responsibility labels that introduce a person's name on a form.
# This pass exists because en_core_web_sm has poor PERSON recall on Indian
# names - it tags 'Harish Chandran' as GPE and 'Ganesh Murthy' as nothing at
# all. Structure is more reliable than NER here: whatever sits in the value
# position of a 'Prepared by' field is a person's name regardless of what the
# model thinks of the tokens. See README "PII masking".
_FORM_LABEL = r"(?:Prepared|Approved|Reviewed|Raised|Issued|Signed|Checked|Authorised|Verified)\s+by"
_NAME_VALUE = r"([A-Z][a-z'\-]+(?:\s+[A-Z][a-z'\-]+)+)"

_FORM_FIELD_RES = [
    # | Prepared by | Harish Chandran | ...
    re.compile(rf"^\|\s*{_FORM_LABEL}\s*\|\s*{_NAME_VALUE}\s*(?=\|)", re.M),
    # Prepared by: Harish Chandran
    re.compile(rf"{_FORM_LABEL}\s*[:\-]\s*{_NAME_VALUE}"),
    # | Operator | Harish Chandran |   /   | Contact | ... |
    re.compile(rf"^\|\s*(?:Operator|Owner|Contact|Signatory|Name)\s*\|\s*{_NAME_VALUE}\s*(?=\|)", re.M),
]


def _form_field_names(text: str):
    """Detect person names sitting in the value position of a labelled field."""
    for rx in _FORM_FIELD_RES:
        for m in rx.finditer(text):
            yield _Detection(
                start=m.start(1), end=m.end(1), entity_type="PERSON", score=0.9
            )


def _propagate(text: str, chosen: list, protect: list[tuple[int, int]]):
    """Mask every other occurrence of a literal we already decided is PII.

    One detection anywhere in the document should protect all of its mentions;
    otherwise a name caught in a signature block still leaks from the prose
    two lines down.
    """
    extra: list[_Detection] = []
    taken = [(c.start, c.end) for c in chosen]
    for c in list(chosen):
        literal = text[c.start:c.end]
        if len(literal) < 4:
            continue
        for m in re.finditer(re.escape(literal), text):
            span = (m.start(), m.end())
            if any(not (span[1] <= s or span[0] >= e) for s, e in taken):
                continue
            if not _keep_detection(text, span[0], span[1], c.entity_type, protect):
                continue
            extra.append(_Detection(span[0], span[1], c.entity_type, 0.95))
            taken.append(span)
    return extra


def _table_cells(text: str):
    """Yield (absolute_offset, cell_text) for every markdown table cell."""
    pos = 0
    for line in text.splitlines(keepends=True):
        if line.count("|") >= 2:
            cursor = pos
            for part in line.split("|"):
                yield cursor, part
                cursor += len(part) + 1
        pos += len(line)


@dataclass
class MaskResult:
    text: str
    mapping: dict[str, str] = field(default_factory=dict)  # placeholder -> original
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def masked_any(self) -> bool:
        return bool(self.mapping)


def protected_spans(text: str, extra_literals=()) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for rx in PROTECT_PATTERNS:
        spans.extend((m.start(), m.end()) for m in rx.finditer(text))
    low = text.lower()
    for lit in list(PROTECT_LITERALS) + list(extra_literals):
        if not lit or len(lit) < 3:
            continue
        start = 0
        ll = lit.lower()
        while True:
            i = low.find(ll, start)
            if i < 0:
                break
            spans.append((i, i + len(lit)))
            start = i + 1
    return spans


def heading_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of markdown heading lines and table header rows.

    A heading is a title, never a signature field, so a PERSON detection
    landing in one is a false positive by construction. Without this, spaCy
    tags 'Soak Times' and 'Bolt Torque' as people and the masker destroys the
    document titles the retriever depends on.
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.strip()
        is_heading = stripped.startswith("#")
        # A table header row is the one immediately above a |---|---| divider.
        is_table_header = (
            stripped.startswith("|")
            and i + 1 < len(lines)
            and set(lines[i + 1].strip()) <= set("|-: ")
            and "-" in lines[i + 1]
        )
        if is_heading or is_table_header:
            spans.append((pos, pos + len(line)))
        pos += len(line)
    return spans


# A PERSON span must actually look like a name: two or more capitalised words.
# Without this, spaCy tags bare nouns from shop-floor prose ('Maintenance',
# 'Operator', 'Line') as people and the masker eats the document.
# Literal spaces, not \s: a PERSON span that crosses a line break is an NER
# artefact ('Maintenance Yes\nReset'), never a name on a form.
_NAME_SHAPE = re.compile(r"^[A-Z][a-z'\-]+(?: [A-Z][a-z'\-]+)+$")


def _keep_detection(text: str, start: int, end: int, entity: str,
                    protect: list[tuple[int, int]]) -> bool:
    """Decide whether one detected span survives the protected vocabulary.

    The rule is containment-aware rather than a blunt overlap test. A detection
    is dropped when an overlapping protected span is NOT strictly inside it:

      detection 'Arcflow Castings' vs protected 'Arcflow Castings'
          -> identical, so the detection is really a company name -> drop
      detection 'Line' vs protected 'Line 1'
          -> protected extends past the detection -> drop
      detection 'harish.chandran@ironvale-components.example' vs protected
      'Ironvale' -> protected sits strictly inside -> keep and mask

    That last case is the one a plain overlap test gets wrong, and it is the
    difference between masking an email address and leaking it.
    """
    span = text[start:end]
    if entity == "PERSON" and not _NAME_SHAPE.match(span.strip()):
        return False

    for ps, pe in protect:
        if pe <= start or ps >= end:
            continue  # no overlap
        if entity == "PERSON":
            # No containment exception for names. A PERSON span that merely
            # *contains* protected vocabulary is a false positive - NER returns
            # 'Metsun Fasteners Limited' and 'Shakti Forgings\nDate' by
            # swallowing a company name plus whatever follows it. The email
            # case below is the only place containment should rescue a
            # detection, and an email is never a PERSON.
            return False
        strictly_inside = ps >= start and pe <= end and (ps > start or pe < end)
        if not strictly_inside:
            return False
    return True


@lru_cache(maxsize=1)
def get_analyzer():
    from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    provider = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
    })
    engine = AnalyzerEngine(nlp_engine=provider.create_engine(), supported_languages=["en"])

    # Employee/badge numbers as used on Ironvale shift paperwork.
    engine.registry.add_recognizer(PatternRecognizer(
        supported_entity="IRONVALE_BADGE",
        patterns=[
            Pattern(name="badge", regex=r"\b(?:EMP|BADGE|ID)[-/ ]?\d{4,6}\b", score=0.85),
        ],
    ))

    # Presidio's built-in PHONE_NUMBER only looks at a configured region list
    # (US/UK by default), so Indian numbers sail straight through. The plant is
    # in Hosur, so +91 numbers are the normal case here.
    engine.registry.add_recognizer(PatternRecognizer(
        supported_entity="PHONE_NUMBER",
        patterns=[
            Pattern(name="in_phone_intl",
                    regex=r"\+91[\s-]?\d{5}[\s-]?\d{5}\b", score=0.9),
            Pattern(name="in_phone_local",
                    regex=r"\b0?[6-9]\d{4}[\s-]?\d{5}\b", score=0.6),
        ],
    ))

    # Belt and braces on email: the built-in recognizer is good, but this makes
    # the behaviour independent of the NLP engine being available.
    engine.registry.add_recognizer(PatternRecognizer(
        supported_entity="EMAIL_ADDRESS",
        patterns=[
            Pattern(name="email",
                    regex=r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b", score=0.9),
        ],
    ))
    return engine


def mask_text(text: str, enabled: bool = True, extra_protect=()) -> MaskResult:
    """Replace PII with stable placeholders. Returns text + the mapping.

    `extra_protect` takes document-specific vocabulary that must survive -
    ingest passes every corpus title, because an NER model will happily decide
    that 'Soak Times' is a person.
    """
    if not enabled or not text.strip():
        return MaskResult(text=text)

    try:
        analyzer = get_analyzer()
        results = list(analyzer.analyze(text=text, language="en", entities=MASK_ENTITIES))

        # Markdown table rows read as one long pipe-separated line, and the NER
        # model misses names inside them ('| Approved by | Harish Chandran |
        # ... |'). Re-analyse each cell as its own short context and merge the
        # hits back at the right offsets. Cheap, and it catches what the
        # document-level pass drops.
        for cell_start, cell_text in _table_cells(text):
            if len(cell_text.strip()) < 3:
                continue
            for r in analyzer.analyze(
                text=cell_text, language="en", entities=MASK_ENTITIES
            ):
                results.append(_Detection(
                    start=cell_start + r.start,
                    end=cell_start + r.end,
                    entity_type=r.entity_type,
                    score=r.score,
                ))
    except Exception:
        # Masking must never be the reason ingest fails. If the NLP engine is
        # unavailable we fall back to the regex-only recognizers below.
        results = []
        for rx, ent in (
            (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "EMAIL_ADDRESS"),
            (re.compile(r"\+?\d[\d\s-]{8,14}\d"), "PHONE_NUMBER"),
            (re.compile(r"\b(?:EMP|BADGE|ID)[-/ ]?\d{4,6}\b", re.I), "IRONVALE_BADGE"),
        ):
            for m in rx.finditer(text):
                results.append(type("R", (), {
                    "start": m.start(), "end": m.end(), "entity_type": ent, "score": 0.8,
                })())

    # Structure-based pass, independent of NER recall.
    results.extend(_form_field_names(text))

    protect = protected_spans(text, extra_protect)
    headings = heading_spans(text)

    def in_heading(start: int, end: int) -> bool:
        return any(start >= s and end <= e for s, e in headings)

    keep = [
        r for r in results
        if getattr(r, "score", 1.0) >= 0.5
        and _keep_detection(text, r.start, r.end, r.entity_type, protect)
        # Titles are not signature fields: drop PERSON hits inside headings.
        and not (r.entity_type == "PERSON" and in_heading(r.start, r.end))
    ]
    if not keep:
        return MaskResult(text=text)

    # Longest-first, non-overlapping, applied right to left.
    keep.sort(key=lambda r: (r.start, -(r.end - r.start)))
    chosen: list = []
    for r in keep:
        if chosen and r.start < chosen[-1].end:
            continue
        chosen.append(r)

    # Now that we know what counts as PII here, catch its other mentions.
    chosen.extend(_propagate(text, chosen, protect))
    chosen.sort(key=lambda r: r.start)

    mapping: dict[str, str] = {}
    counts: dict[str, int] = {}
    by_value: dict[tuple[str, str], str] = {}
    out = text
    for r in reversed(chosen):
        original = text[r.start:r.end]
        key = (r.entity_type, original.lower())
        if key in by_value:
            ph = by_value[key]
        else:
            counts[r.entity_type] = counts.get(r.entity_type, 0) + 1
            ph = f"<{r.entity_type}_{counts[r.entity_type]}>"
            by_value[key] = ph
            mapping[ph] = original
        out = out[:r.start] + ph + out[r.end:]

    return MaskResult(text=out, mapping=mapping, counts=counts)

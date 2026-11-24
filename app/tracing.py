"""Tracing: one trace per request, with the span names the brief specifies.

Spans: understand, retrieve.bm25, retrieve.dense, fuse, rerank, generate,
sql.generate, sql.validate, sql.execute, graph.fill, graph.query

Two sinks, always both:
  * self-hosted Langfuse, when LANGFUSE_ENABLED and reachable
  * a local JSONL mirror at TRACE_FILE

The mirror is not a convenience. The leak tests assert on what a trace contains
- in particular that a denied price question produced a trace in which
  PRC-2026-Q3 never appears - and asserting that against a running Langfuse
  would make the test suite depend on a web service.

What may be recorded is an explicit allow-list, not a deny-list. Anything not
named in ALLOWED_FIELDS is dropped, so a future field cannot leak by being
forgotten. On top of that, `scrub` rejects values that look like a connection
string, an API key or a placeholder map even if the key was allowed, and
redacts the text of any document whose id is not openable by every role.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.config import settings

SPAN_NAMES = (
    "understand",
    "retrieve.bm25",
    "retrieve.dense",
    "fuse",
    "rerank",
    "generate",
    "sql.generate",
    "sql.validate",
    "sql.execute",
    "graph.fill",
    "graph.query",
)

# The only metadata keys that are ever written to a trace.
ALLOWED_FIELDS = frozenset({
    # identity - user_id only, never the display name
    "user_id", "role", "scope",
    # routing and outcome
    "route", "status", "error_class", "prompt_version", "model",
    "question", "rewritten_query",
    # cost and size
    "prompt_tokens", "completion_tokens", "total_tokens", "cost_inr", "cached",
    "latency_ms", "duration_ms", "attempts",
    # retrieval
    "chunk_ids", "n_bm25", "n_dense", "n_fused", "rerank_scores",
    "top_rerank_score", "threshold", "scopes", "only_superseded",
    "cited_doc_ids", "guards_fired", "candidates",
    # sql
    "sql", "row_count", "columns", "chart_type", "rejected_reason", "repaired",
    # graph
    "graph_template", "graph_params", "path_count", "node_count", "edge_count",
    # feedback
    "thumbs", "comment", "request_id", "trace_id",
})

# Values that must never appear, whatever key they arrive under.
_FORBIDDEN_VALUE = re.compile(
    r"(postgresql://|postgres://|bolt://|neo4j://|"
    r"sk-[A-Za-z0-9_\-]{8,}|pk-lf-|sk-lf-|"
    r"password\s*=|PGPASSWORD)",
    re.I,
)
# Placeholder-map shaped content.
_PLACEHOLDER_MAP = re.compile(r"<(?:PERSON|EMAIL_ADDRESS|PHONE_NUMBER|IRONVALE_BADGE)_\d+>\s*[:=]")

_REDACTED = "[redacted]"


def _restricted_doc_ids() -> set[str]:
    """Documents not openable by every role - their text must never be traced."""
    try:
        from app.retrieval.store import doc_access_map

        amap = doc_access_map()
    except Exception:
        return set()
    return {d for d, roles in amap.items() if len(set(roles)) < 3}


def scrub(value: Any, depth: int = 0) -> Any:
    """Recursively drop anything that must not be traced."""
    if depth > 6:
        return _REDACTED
    if isinstance(value, dict):
        return {k: scrub(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, depth + 1) for v in value]
    if isinstance(value, str):
        if _FORBIDDEN_VALUE.search(value) or _PLACEHOLDER_MAP.search(value):
            return _REDACTED
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:500]


def filter_fields(data: dict) -> dict:
    """Keep only allow-listed keys, then scrub the values."""
    if not data:
        return {}
    out = {}
    for k, v in data.items():
        if k not in ALLOWED_FIELDS:
            continue
        out[k] = scrub(v)
    return out


@dataclass
class Span:
    name: str
    started: float
    ended: float | None = None
    data: dict = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        return ((self.ended or time.perf_counter()) - self.started) * 1000

    def to_dict(self) -> dict:
        d = dict(self.data)
        d["duration_ms"] = round(self.duration_ms, 2)
        return {"name": self.name, **filter_fields(d)}


_LF_LOCK = threading.Lock()
_LANGFUSE = None
_LANGFUSE_TRIED = False


def _langfuse():
    """The Langfuse client, or None. Never raises: tracing must not break a request."""
    global _LANGFUSE, _LANGFUSE_TRIED
    if not settings.langfuse_enabled:
        return None
    with _LF_LOCK:
        if _LANGFUSE_TRIED:
            return _LANGFUSE
        _LANGFUSE_TRIED = True
        if not (settings.langfuse_public_key and settings.langfuse_secret_key):
            return None
        try:
            from langfuse import Langfuse

            _LANGFUSE = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_host,
            )
        except Exception:
            _LANGFUSE = None
        return _LANGFUSE


class Trace:
    """One request. Use as a context manager; spans via `with tr.span(name):`."""

    def __init__(self, request_id: str, user_id: str, question: str):
        self.request_id = request_id
        self.trace_id = uuid.uuid4().hex
        self.user_id = user_id
        self.question = question
        self.started = time.perf_counter()
        self.spans: list[Span] = []
        self.meta: dict = {}
        self._lf_trace = None
        self._open: list[Span] = []

        lf = _langfuse()
        if lf is not None:
            try:
                self._lf_trace = lf.trace(
                    id=self.trace_id,
                    name="ask",
                    user_id=user_id,          # user_id, never the display name
                    input={"question": question},
                    metadata={"request_id": request_id},
                    tags=[f"prompt:{settings.prompt_version}"],
                )
            except Exception:
                self._lf_trace = None

    # ------------------------------------------------------------ spans
    def span(self, name: str):
        if name not in SPAN_NAMES:
            raise ValueError(f"unknown span name {name!r}; add it to SPAN_NAMES")
        return _SpanCtx(self, name)

    def _start(self, name: str) -> Span:
        s = Span(name=name, started=time.perf_counter())
        self.spans.append(s)
        self._open.append(s)
        return s

    def _end(self, s: Span) -> None:
        s.ended = time.perf_counter()
        if s in self._open:
            self._open.remove(s)
        if self._lf_trace is not None:
            try:
                self._lf_trace.span(
                    name=s.name,
                    start_time=None,
                    metadata=filter_fields(s.data),
                )
            except Exception:
                pass

    # ------------------------------------------------------------ record
    def set(self, **kwargs) -> None:
        self.meta.update(kwargs)

    @property
    def timings_ms(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for s in self.spans:
            out[s.name] = round(out.get(s.name, 0.0) + s.duration_ms, 2)
        out["total"] = round((time.perf_counter() - self.started) * 1000, 2)
        return out

    def to_dict(self) -> dict:
        restricted = _restricted_doc_ids()
        meta = filter_fields({
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "user_id": self.user_id,
            "question": self.question,
            "prompt_version": settings.prompt_version,
            **self.meta,
        })
        # Final backstop: a restricted doc id may be recorded (that is how we
        # prove it was NOT retrieved), but never any text from one.
        body = {
            "ts": time.time(),
            "trace": meta,
            "spans": [s.to_dict() for s in self.spans],
            "timings_ms": self.timings_ms,
            "restricted_docs_known": sorted(restricted),
        }
        return body

    def flush(self) -> None:
        body = self.to_dict()
        try:
            settings.trace_file.parent.mkdir(parents=True, exist_ok=True)
            with settings.trace_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(body, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

        if self._lf_trace is not None:
            try:
                self._lf_trace.update(
                    output={
                        "status": self.meta.get("status"),
                        "route": self.meta.get("route"),
                    },
                    metadata=body["trace"],
                )
                lf = _langfuse()
                if lf is not None:
                    lf.flush()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for s in list(self._open):
            self._end(s)
        self.flush()
        return False


class _SpanCtx:
    def __init__(self, trace: Trace, name: str):
        self.trace = trace
        self.name = name
        self.span: Span | None = None

    def __enter__(self) -> Span:
        self.span = self.trace._start(self.name)
        return self.span

    def __exit__(self, *exc):
        if self.span is not None:
            self.trace._end(self.span)
        return False


def score_feedback(trace_id: str, value: str, comment: str | None = None) -> bool:
    """Send a thumbs up/down to Langfuse as a score, and mirror it locally."""
    ok = False
    lf = _langfuse()
    if lf is not None:
        try:
            lf.score(
                trace_id=trace_id,
                name="thumbs",
                value=1 if value == "up" else 0,
                comment=(comment or None),
            )
            lf.flush()
            ok = True
        except Exception:
            ok = False
    try:
        with settings.trace_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": time.time(),
                "feedback": filter_fields(
                    {"trace_id": trace_id, "thumbs": value, "comment": comment}
                ),
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return ok


def langfuse_trace_url(trace_id: str) -> str:
    return f"{settings.langfuse_host.rstrip('/')}/trace/{trace_id}"

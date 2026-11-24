"""The public response contract for POST /v1/ask.

Nothing outside this module is allowed to shape what a client sees.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Route = Literal["docs", "data", "graph", "out_of_scope"]
Status = Literal["answered", "not_found", "out_of_scope", "error"]
ChartType = Literal["bar", "line", "table"]


class Citation(BaseModel):
    """One [n] marker in the answer. `n` is 1-based and matches the answer text."""

    n: int
    doc_id: str
    title: str
    section: str | None = None
    page: int | None = Field(
        default=None,
        description="Printed page number as it appears in the document footer, "
        "not the 0-based PDF page index.",
    )
    version: str | None = None
    effective_date: str | None = None
    superseded: bool = False
    source: Literal["digital", "scanned"] = "digital"
    chunk_id: str
    snippet: str = ""


class Chart(BaseModel):
    type: ChartType
    x: str | None = None
    y: str | None = None


class SqlResult(BaseModel):
    sql: str
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    chart: Chart | None = None
    metric_note: str | None = Field(
        default=None,
        description="Set when the question used a metric we had to define ourselves.",
    )


class GraphResult(BaseModel):
    template: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    paths: list[list[str]] = Field(default_factory=list)
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    path_count: int = 0


class Pipeline(BaseModel):
    timings_ms: dict[str, float] = Field(default_factory=dict)
    retrieved_bm25: int = 0
    retrieved_dense: int = 0
    retrieved_fused: int = 0
    reranked: int = 0
    top_rerank_score: float | None = None
    model: str | None = None
    prompt_version: str | None = None
    trace_id: str | None = None
    error_class: str | None = None


class AskRequest(BaseModel):
    """Body of POST /v1/ask. Note there is no role field - role is server-side only."""

    model_config = {"extra": "forbid"}

    user_id: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=2000)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=50)

    @field_validator("history")
    @classmethod
    def _check_history(cls, v: list[dict[str, str]]) -> list[dict[str, str]]:
        for turn in v:
            if "role" not in turn or "content" not in turn:
                raise ValueError("each history turn needs 'role' and 'content'")
            if turn["role"] not in {"user", "assistant"}:
                raise ValueError("history role must be 'user' or 'assistant'")
        return v


class AskResponse(BaseModel):
    request_id: str
    user_id: str
    role: str | None = None
    question: str
    rewritten_query: str | None = None
    route: Route | None = None
    status: Status
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    sql: SqlResult | None = None
    graph: GraphResult | None = None
    pipeline: Pipeline = Field(default_factory=Pipeline)


class UserOut(BaseModel):
    user_id: str
    name: str
    role: str
    scope: str


class FeedbackRequest(BaseModel):
    model_config = {"extra": "forbid"}

    request_id: str = Field(min_length=1, max_length=64)
    trace_id: str | None = None
    value: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=1000)

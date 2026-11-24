"""Hybrid retrieval: BM25 + dense, fused with RRF, then cross-encoder reranked.

Pipeline, matching the brief:
    BM25 top 20  (access filter applied before any scoring)
    dense top 20 (access filter inside the SQL WHERE clause)
    -> RRF, k=60
    -> bge-reranker-base on CPU, keep top 5
    -> if the top rerank score is below the calibrated threshold: not_found

Version preference: current documents are boosted over superseded ones unless
the question is explicitly historical, and if the only thing that matched is
superseded we say so and report the current version's effective date.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from app.config import settings
from app.db import admin_conn
from app.retrieval import store
from app.retrieval.embed import embed_query, rerank
from app.retrieval.focus import focus_excerpt
from app.retrieval.tokenize import tokenize

# Questions that are actually about history, where a superseded version is the
# correct answer rather than a stale one.
HISTORY_RE = re.compile(
    r"\b(v\d+|version \d+|previous|prior|old|older|earlier|before|used to|"
    r"superseded|history|historical|back then|at the time|what did .* say)\b",
    re.I,
)

SUPERSEDED_PENALTY = 0.65   # multiplies the RRF score of a superseded chunk


@dataclass
class Hit:
    chunk: store.ChunkRow
    bm25_rank: int | None = None
    dense_rank: int | None = None
    bm25_score: float = 0.0
    dense_score: float = 0.0
    rrf: float = 0.0
    rerank_score: float | None = None


@dataclass
class RetrievalResult:
    hits: list[Hit] = field(default_factory=list)
    n_bm25: int = 0
    n_dense: int = 0
    n_fused: int = 0
    top_rerank_score: float | None = None
    only_superseded: bool = False
    current_version_note: str | None = None
    timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def chunk_ids(self) -> list[str]:
        return [h.chunk.chunk_id for h in self.hits]


def is_history_question(text: str) -> bool:
    return bool(HISTORY_RE.search(text or ""))


# ------------------------------------------------------------------ retrievers

def bm25_search(query: str, role: str, scopes, top_k: int) -> list[tuple[store.ChunkRow, float]]:
    """Sparse retrieval over the access slice only."""
    rows, index = store.bm25_for(role, scopes)
    if not rows or index is None:
        return []
    toks = tokenize(query)
    if not toks:
        return []
    scores = index.get_scores(toks)
    order = sorted(range(len(rows)), key=lambda i: -scores[i])[:top_k]
    return [(rows[i], float(scores[i])) for i in order if scores[i] > 0.0]


def dense_search(query: str, role: str, scopes, top_k: int) -> list[tuple[store.ChunkRow, float]]:
    """Dense retrieval in pgvector, with role and scope in the WHERE clause."""
    vec = embed_query(query)
    lit = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"

    sql = [
        "SELECT chunk_id, 1 - (embedding <=> %s::vector) AS sim",
        "FROM doc_chunks",
        "WHERE %s = ANY(allowed_roles)",
    ]
    params: list = [lit, role]
    if scopes:
        sql.append("AND scope = ANY(%s)")
        params.append(list(scopes))
    sql.append("ORDER BY embedding <=> %s::vector LIMIT %s")
    params.extend([lit, top_k])

    with admin_conn() as conn, conn.cursor() as cur:
        cur.execute("\n".join(sql), params)
        rows = cur.fetchall()

    out = []
    for chunk_id, sim in rows:
        c = store.get(chunk_id)
        if c is None:
            continue
        # Defence in depth: the SQL already filtered, assert it anyway.
        if role not in c.allowed_roles:
            raise AssertionError(f"dense search returned inaccessible chunk {chunk_id}")
        out.append((c, float(sim)))
    return out


def rrf_fuse(bm25: list, dense: list, k: int, history: bool) -> list[Hit]:
    """Reciprocal rank fusion. score = sum over lists of 1/(k + rank)."""
    hits: dict[str, Hit] = {}

    for rank, (chunk, score) in enumerate(bm25, start=1):
        h = hits.setdefault(chunk.chunk_id, Hit(chunk=chunk))
        h.bm25_rank, h.bm25_score = rank, score
        h.rrf += 1.0 / (k + rank)

    for rank, (chunk, score) in enumerate(dense, start=1):
        h = hits.setdefault(chunk.chunk_id, Hit(chunk=chunk))
        h.dense_rank, h.dense_score = rank, score
        h.rrf += 1.0 / (k + rank)

    # Prefer the current version unless the question is about history.
    if not history:
        for h in hits.values():
            if h.chunk.superseded:
                h.rrf *= SUPERSEDED_PENALTY

    return sorted(hits.values(), key=lambda h: -h.rrf)


def current_version_of(doc_id: str) -> tuple[str | None, str | None]:
    """(version, effective_date) of the live document that superseded this one."""
    with admin_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT title FROM documents WHERE doc_id = %s", (doc_id,))
        row = cur.fetchone()
        if not row:
            return None, None
        cur.execute(
            "SELECT version, effective_date FROM documents"
            " WHERE title = %s AND NOT superseded ORDER BY effective_date DESC LIMIT 1",
            (row[0],),
        )
        cur_row = cur.fetchone()
    if not cur_row:
        return None, None
    return cur_row[0], (cur_row[1].isoformat() if cur_row[1] else None)


# ------------------------------------------------------------------- the entry

def retrieve(
    query: str,
    role: str,
    scopes: list[str] | None = None,
    top_k: int | None = None,
    rerank_top_n: int | None = None,
    history: bool | None = None,
) -> RetrievalResult:
    top_k = top_k or settings.retrieve_top_k
    rerank_top_n = rerank_top_n or settings.rerank_top_n
    if history is None:
        history = is_history_question(query)

    res = RetrievalResult()

    t0 = time.perf_counter()
    sparse = bm25_search(query, role, scopes, top_k)
    res.timings_ms["retrieve.bm25"] = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    dense = dense_search(query, role, scopes, top_k)
    res.timings_ms["retrieve.dense"] = (time.perf_counter() - t0) * 1000

    res.n_bm25, res.n_dense = len(sparse), len(dense)

    t0 = time.perf_counter()
    fused = rrf_fuse(sparse, dense, settings.rrf_k, history)
    res.timings_ms["fuse"] = (time.perf_counter() - t0) * 1000
    res.n_fused = len(fused)

    if not fused:
        return res

    # Rerank a bounded candidate set, on a query-focused excerpt of each chunk.
    # Both bounds are there for latency: this cross-encoder costs ~200-860ms per
    # pair on CPU depending on length, so scoring every fused candidate at full
    # length would put a question at 25s+.
    candidates = fused[: settings.rerank_candidates]
    passages = [
        focus_excerpt(h.chunk.text, query, settings.rerank_max_tokens)
        for h in candidates
    ]
    t0 = time.perf_counter()
    scores = rerank(query, passages)
    res.timings_ms["rerank"] = (time.perf_counter() - t0) * 1000

    for h, s in zip(candidates, scores):
        h.rerank_score = s

    # Keep the reranker's order; break ties with the fusion score.
    candidates.sort(key=lambda h: (-(h.rerank_score or 0.0), -h.rrf))
    kept = candidates[:rerank_top_n]
    res.hits = kept
    res.top_rerank_score = kept[0].rerank_score if kept else None

    if kept and all(h.chunk.superseded for h in kept):
        res.only_superseded = True
        ver, eff = current_version_of(kept[0].chunk.doc_id)
        if ver:
            res.current_version_note = (
                f"The only matching text is from a superseded version. "
                f"The current version is {ver}, effective {eff}."
            )

    return res

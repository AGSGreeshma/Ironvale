"""The chunk store, and the access filter that every retrieval path goes through.

The corpus is small (a few hundred chunks), so chunks are loaded into memory
once and the sparse index is built per access slice. That is not a shortcut - it
is what makes the access guarantee easy to state and easy to test:

    a chunk the user cannot open is never put into the BM25 corpus at all,
    so it cannot be scored, cannot be fused, cannot be reranked and cannot
    reach the model.

The dense side gets the same guarantee a different way: the role and scope
predicates are part of the SQL WHERE clause, so pgvector never returns a
restricted row in the first place. Neither path filters after scoring.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from functools import lru_cache

from app.db import admin_conn
from app.retrieval.tokenize import tokenize


@dataclass
class ChunkRow:
    chunk_id: str
    doc_id: str
    title: str
    section: str | None
    page: int | None
    version: str | None
    effective_date: object
    superseded: bool
    allowed_roles: tuple[str, ...]
    scope: str
    source: str
    ord: int
    token_count: int
    text: str
    had_injection: bool
    tokens: tuple[str, ...] = ()


_LOCK = threading.Lock()
_CHUNKS: list[ChunkRow] | None = None
_BY_ID: dict[str, ChunkRow] = {}
_BM25_CACHE: dict[tuple, object] = {}


def load(force: bool = False) -> list[ChunkRow]:
    global _CHUNKS
    with _LOCK:
        if _CHUNKS is not None and not force:
            return _CHUNKS
        rows: list[ChunkRow] = []
        with admin_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT chunk_id, doc_id, title, section, page, version,"
                " effective_date, superseded, allowed_roles, scope, source, ord,"
                " token_count, text, had_injection"
                " FROM doc_chunks ORDER BY doc_id, ord"
            )
            for r in cur.fetchall():
                row = ChunkRow(
                    chunk_id=r[0], doc_id=r[1], title=r[2], section=r[3], page=r[4],
                    version=r[5], effective_date=r[6], superseded=r[7],
                    allowed_roles=tuple(r[8]), scope=r[9], source=r[10], ord=r[11],
                    token_count=r[12], text=r[13], had_injection=r[14],
                )
                row.tokens = tuple(tokenize(row.text))
                rows.append(row)
        _CHUNKS = rows
        _BY_ID.clear()
        _BY_ID.update({c.chunk_id: c for c in rows})
        _BM25_CACHE.clear()
        return _CHUNKS


def get(chunk_id: str) -> ChunkRow | None:
    load()
    return _BY_ID.get(chunk_id)


def all_chunks() -> list[ChunkRow]:
    return load()


def reset() -> None:
    """Drop caches. Used by tests after a re-ingest."""
    global _CHUNKS
    with _LOCK:
        _CHUNKS = None
        _BY_ID.clear()
        _BM25_CACHE.clear()
    doc_access_map.cache_clear()


@lru_cache(maxsize=1)
def doc_access_map() -> dict[str, tuple[str, ...]]:
    """doc_id -> roles allowed to open it. Used by the graph access filter."""
    with admin_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT doc_id, allowed_roles FROM documents")
        return {r[0]: tuple(r[1]) for r in cur.fetchall()}


# --------------------------------------------------------------- access slice

def access_slice(role: str, scopes: list[str] | None = None) -> list[ChunkRow]:
    """Exactly the chunks this role may open, optionally narrowed by scope.

    This is the only way to obtain chunks for retrieval.
    """
    out = [c for c in load() if role in c.allowed_roles]
    if scopes:
        allowed = set(scopes)
        out = [c for c in out if c.scope in allowed]
    return out


def bm25_for(role: str, scopes: list[str] | None = None):
    """A BM25 index built over the access slice only (cached per slice)."""
    from rank_bm25 import BM25Okapi

    key = (role, tuple(sorted(scopes)) if scopes else None)
    with _LOCK:
        hit = _BM25_CACHE.get(key)
    if hit is not None:
        return hit

    rows = access_slice(role, scopes)
    if not rows:
        built = ([], None)
    else:
        built = (rows, BM25Okapi([list(r.tokens) for r in rows]))
    with _LOCK:
        _BM25_CACHE[key] = built
    return built

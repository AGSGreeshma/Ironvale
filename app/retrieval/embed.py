"""Embeddings and reranking.

Both models are the ones the brief names - BAAI/bge-small-en-v1.5 (384-dim) and
BAAI/bge-reranker-base - served through fastembed, which runs the ONNX exports
on onnxruntime. That keeps it CPU-only and avoids a ~2.5GB torch install for
two small models.

bge expects an instruction prefix on the *query* side only; documents are
embedded bare. Getting that backwards costs a few points of recall, so it is
done in one place here.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np

from app.config import settings

QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
EMBED_DIM = 384


@lru_cache(maxsize=1)
def _embedder():
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=settings.embed_model)


@lru_cache(maxsize=1)
def _reranker():
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder(model_name=settings.rerank_model, threads=settings.rerank_threads)


def _rerank_cache_path() -> "Path":
    from pathlib import Path

    d = Path(settings.llm_cache_dir).parent / "rerank"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_key(query: str, passage: str) -> str:
    import hashlib

    h = hashlib.sha256()
    h.update(settings.rerank_model.encode())
    h.update(b"\x00")
    h.update(query.encode())
    h.update(b"\x00")
    h.update(passage.encode())
    return h.hexdigest()


def embed_documents(texts: list[str], batch_size: int = 32) -> np.ndarray:
    if not texts:
        return np.zeros((0, EMBED_DIM), dtype=np.float32)
    vecs = list(_embedder().embed(texts, batch_size=batch_size))
    return np.asarray(vecs, dtype=np.float32)


def embed_query(text: str) -> np.ndarray:
    vec = next(iter(_embedder().query_embed([text])))
    return np.asarray(vec, dtype=np.float32)


def rerank(query: str, passages: list[str], use_cache: bool | None = None) -> list[float]:
    """Cross-encoder scores, one per passage. Higher is better.

    Raw logits are squashed through a sigmoid here, once, so the not_found
    threshold is calibrated against a stable 0-1 scale.

    Scores are cached on disk by (model, query, passage) when LLM_CACHE is on.
    The reranker is the slowest component in the pipeline, and eval runs and
    demo replays ask the same questions repeatedly; the cache makes those
    near-instant without changing any result.
    """
    if not passages:
        return []

    cache_on = settings.llm_cache if use_cache is None else use_cache
    out: list[float | None] = [None] * len(passages)
    todo: list[int] = []

    if cache_on:
        d = _rerank_cache_path()
        for i, p in enumerate(passages):
            f = d / f"{_cache_key(query, p)}.txt"
            if f.exists():
                try:
                    out[i] = float(f.read_text())
                    continue
                except ValueError:
                    pass
            todo.append(i)
    else:
        todo = list(range(len(passages)))

    if todo:
        raw = list(_reranker().rerank(query, [passages[i] for i in todo]))
        for i, s in zip(todo, raw):
            val = float(1.0 / (1.0 + np.exp(-s)))
            out[i] = val
            if cache_on:
                (_rerank_cache_path() / f"{_cache_key(query, passages[i])}.txt").write_text(
                    repr(val)
                )

    return [float(x if x is not None else 0.0) for x in out]


def warm_up() -> None:
    """Pull both models down and run one tiny inference each."""
    embed_documents(["warm up"])
    rerank("warm up", ["warm up"])

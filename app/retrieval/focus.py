"""Query-focused excerpting for the reranker.

bge-reranker-base is an XLM-R-base cross-encoder (278M parameters). Measured on
the CPU this runs on, one query/passage pair costs ~200ms at 96 tokens and
~860ms at 512, so scoring 27 full 500-token chunks takes ~25 seconds. Nearly
all of that is spent on text that has nothing to do with the question: a chunk
is 400-600 tokens by design, while the sentence that answers the question is
usually one or two lines.

So the reranker is shown a window of the chunk centred on the query's own
terms, capped at `max_tokens`. The heading line is always kept so the passage
is still identifiable.

This also sharpens the thing the brief cares most about. The E-417 and E-471
sentences live in the same chunk of MAN-CNC-04; scoring the whole chunk gives
one score that cannot distinguish them, whereas scoring the window around the
matched code scores exactly the sentence that was asked about.
"""
from __future__ import annotations

from app.ingest.chunk import count_tokens
from app.retrieval.tokenize import exact_tokens, tokenize

DEFAULT_MAX_TOKENS = 256


def _line_scores(lines: list[str], q_exact: set[str], q_all: set[str]) -> list[float]:
    scores = []
    for ln in lines:
        lt = set(tokenize(ln))
        if not lt:
            scores.append(0.0)
            continue
        # An identifier match (E-417, HX-220-B) is worth far more than an
        # ordinary word match - it is the difference between the right alarm
        # and its neighbour.
        exact_hits = len(lt & q_exact)
        word_hits = len(lt & q_all)
        scores.append(exact_hits * 10.0 + word_hits)
    return scores


def focus_excerpt(text: str, query: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
    """The most query-relevant window of `text`, within a token budget."""
    if count_tokens(text) <= max_tokens:
        return text

    lines = [ln for ln in text.splitlines()]
    if not lines:
        return text

    q_exact = set(exact_tokens(query))
    q_all = set(tokenize(query))
    scores = _line_scores(lines, q_exact, q_all)
    costs = [max(1, count_tokens(ln)) for ln in lines]

    # Keep the leading heading line as context, and budget for it.
    head_idx = 0
    while head_idx < len(lines) and not lines[head_idx].strip():
        head_idx += 1
    head = lines[head_idx] if head_idx < len(lines) else ""
    head_cost = count_tokens(head) if head.strip().startswith(("#", "|")) or head_idx == 0 else 0
    budget = max(32, max_tokens - head_cost)

    # Best contiguous window of lines under the budget (two pointers).
    best = (-1.0, 0, 0)
    start = 0
    run_cost = 0.0
    run_score = 0.0
    for end in range(len(lines)):
        run_cost += costs[end]
        run_score += scores[end]
        while run_cost > budget and start < end:
            run_cost -= costs[start]
            run_score -= scores[start]
            start += 1
        if run_score > best[0]:
            best = (run_score, start, end)

    _, s, e = best
    window = lines[s:e + 1]

    if head_cost and not (s <= head_idx <= e):
        window = [head, "..."] + window

    out = "\n".join(window).strip()
    return out or text[:4000]

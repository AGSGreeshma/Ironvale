"""Ingest: corpus -> parsed -> masked -> chunked -> embedded -> Postgres.

Run with:  python -m app.ingest.pipeline            (full rebuild)
           python -m app.ingest.pipeline --only SOP-L3-007

Both indexes are written from the same chunk rows and therefore share chunk
ids, which is what makes reciprocal-rank fusion meaningful.

The placeholder map produced by PII masking is written to .index/ on disk,
outside the database and outside any trace, and is not needed to answer
anything - it exists only so a human could re-identify a redaction if an
incident ever required it.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from app.config import settings
from app.db import admin_conn
from app.ingest.chunk import chunk_pages
from app.ingest.parsers import parse
from app.retrieval.embed import embed_documents


def load_manifest() -> list[dict]:
    path = settings.corpus_dir / "manifest.json"
    if not path.exists():
        raise SystemExit(
            f"{path} not found - run `python scripts/gen_corpus.py` first"
        )
    return json.loads(path.read_text(encoding="utf-8"))["documents"]


def vec_literal(v) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def ingest(only: list[str] | None = None, mask: bool = True) -> dict:
    docs = load_manifest()
    if only:
        wanted = set(only)
        docs = [d for d in docs if d["doc_id"] in wanted]
        if not docs:
            raise SystemExit(f"no manifest entry matches {only}")

    # Every title in the corpus is protected vocabulary during masking. NER
    # cannot tell 'Bolt Torque' from a person's name, and a masked title breaks
    # both retrieval and citations.
    protect_terms: list[str] = []
    for d in load_manifest():
        protect_terms.append(d["title"])
        protect_terms.extend(
            part.strip() for part in d["title"].replace(":", "-").split("-")
            if len(part.strip()) > 3
        )
    protect_terms = sorted({t for t in protect_terms if len(t) > 3})

    placeholder_maps: dict[str, dict[str, str]] = {}
    stats = {
        "documents": 0, "chunks": 0, "scanned_pages": 0,
        "injection_chunks": 0, "masked_entities": 0, "t_parse": 0.0,
        "t_embed": 0.0,
    }

    with admin_conn() as conn:
        with conn.cursor() as cur:
            if only:
                cur.executemany(
                    "DELETE FROM documents WHERE doc_id = %s", [(d["doc_id"],) for d in docs]
                )
            else:
                cur.execute("TRUNCATE doc_chunks, documents CASCADE")

            for d in docs:
                doc_id = d["doc_id"]
                path = settings.corpus_dir / d["path"]
                if not path.exists():
                    print(f"  !! missing file for {doc_id}: {path}", file=sys.stderr)
                    continue

                t0 = time.perf_counter()
                pages = parse(path, fmt=d.get("fmt"), page_map=d.get("pages"))
                chunks, pmap = chunk_pages(
                    doc_id, pages, mask_enabled=mask, protect_terms=protect_terms
                )
                stats["t_parse"] += time.perf_counter() - t0

                if not chunks:
                    print(f"  !! no chunks produced for {doc_id}", file=sys.stderr)
                    continue

                if pmap:
                    placeholder_maps[doc_id] = pmap
                    stats["masked_entities"] += len(pmap)
                if d["source"] == "scanned":
                    stats["scanned_pages"] += len(pages)

                cur.execute(
                    "INSERT INTO documents (doc_id, title, version, effective_date,"
                    " superseded, superseded_on, allowed_roles, scope, source, fmt,"
                    " path, filler)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        doc_id, d["title"], d.get("version"), d.get("effective_date"),
                        bool(d.get("superseded")), d.get("superseded_on"),
                        d["allowed_roles"], d["scope"], d["source"], d["fmt"],
                        d["path"], bool(d.get("filler")),
                    ),
                )

                t0 = time.perf_counter()
                vectors = embed_documents([c.text for c in chunks])
                stats["t_embed"] += time.perf_counter() - t0

                rows = []
                for c, v in zip(chunks, vectors):
                    if c.had_injection:
                        stats["injection_chunks"] += 1
                    rows.append((
                        c.chunk_id, doc_id, d["title"], c.section, c.page,
                        d.get("version"), d.get("effective_date"),
                        bool(d.get("superseded")), d["allowed_roles"], d["scope"],
                        c.source, c.ord, c.token_count, c.text, c.had_injection,
                        c.text, vec_literal(v),
                    ))

                cur.executemany(
                    "INSERT INTO doc_chunks (chunk_id, doc_id, title, section, page,"
                    " version, effective_date, superseded, allowed_roles, scope,"
                    " source, ord, token_count, text, had_injection, tsv, embedding)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                    " to_tsvector('english', %s), %s::vector)",
                    rows,
                )

                stats["documents"] += 1
                stats["chunks"] += len(chunks)
                flag = " [injection stripped]" if any(c.had_injection for c in chunks) else ""
                print(f"  {doc_id:<16} {d['source']:<8} {len(chunks):>3} chunks{flag}")

        conn.commit()

    # Placeholder map: on disk, outside the index, never traced.
    if placeholder_maps:
        out = settings.index_dir / "placeholder_map.json"
        out.write_text(json.dumps(placeholder_maps, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        print(f"\nplaceholder map -> {out} ({stats['masked_entities']} entities, "
              f"{len(placeholder_maps)} documents)")

    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ingest the Ironvale corpus.")
    ap.add_argument("--only", nargs="*", help="ingest just these doc_ids")
    ap.add_argument("--no-mask", action="store_true", help="skip PII masking (debug)")
    args = ap.parse_args(argv)

    print("ingesting corpus ...")
    t0 = time.perf_counter()
    s = ingest(only=args.only, mask=not args.no_mask)
    dt = time.perf_counter() - t0

    print(
        f"\n{s['documents']} documents, {s['chunks']} chunks in {dt:.1f}s"
        f"  (parse+ocr {s['t_parse']:.1f}s, embed {s['t_embed']:.1f}s)"
    )
    print(f"  scanned pages OCR'd : {s['scanned_pages']}")
    print(f"  chunks w/ injection : {s['injection_chunks']}")
    print(f"  PII entities masked : {s['masked_entities']}")

    with admin_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT source, count(*), min(token_count), round(avg(token_count)),"
            " max(token_count) FROM doc_chunks GROUP BY source ORDER BY source"
        )
        print("\n  source    chunks  min  avg  max tokens")
        for src, n, mn, avg, mx in cur.fetchall():
            print(f"  {src:<9} {n:>6} {mn:>4} {int(avg):>4} {mx:>4}")
        cur.execute("SELECT count(*) FROM doc_chunks WHERE embedding IS NULL")
        missing = cur.fetchone()[0]
        if missing:
            print(f"  !! {missing} chunks have no embedding")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

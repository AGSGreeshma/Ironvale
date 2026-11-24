# IronAsk (thin slice)

Run: `python seed.py` then `uvicorn app:app --port 8080` then (new shell) `streamlit run ui.py`
Verify: `python check.py` — runs 51 acceptance checks in-process, no server needed.
Mocked/skipped vs the full brief: no OCR (scanned pages are text with printed page numbers), no embeddings/reranker (BM25 keyword only), no Postgres/RLS (SQLite + templated SQL with row scope in every query), no Neo4j (in-memory edge list with `source_doc_ids`), no Langfuse/RAGAS.
LLM is optional: unset `LLM_API_KEY` and answers are templates quoting the retrieved chunk; set it and the chunk is rephrased, never consulted for facts, never blocking (6s timeout, falls back to template).
Enforced regardless: role comes from the users table only, access filtering happens before retrieval, denied is byte-identical to missing, the injected `Assistant:` line is stripped at load, and "412" never reaches a non-head user.

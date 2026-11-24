"""Central configuration. Everything comes from the environment (.env)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

IST = timezone(timedelta(hours=5, minutes=30))


def _b(key: str, default: str = "0") -> bool:
    return os.getenv(key, default).strip().lower() in {"1", "true", "yes", "on"}


def _i(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _f(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


class Settings:
    """Plain object (not BaseSettings) so it stays import-cheap and testable."""

    # ---- Postgres
    pg_host = os.getenv("POSTGRES_HOST", "localhost")
    pg_port = _i("POSTGRES_PORT", 5433)
    pg_db = os.getenv("POSTGRES_DB", "ironask")
    pg_user = os.getenv("POSTGRES_USER", "ironask")
    pg_password = os.getenv("POSTGRES_PASSWORD", "ironask_dev_pw")
    pg_role_password = os.getenv("PG_ROLE_PASSWORD", "ironask_role_pw")

    # ---- Neo4j
    neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7688")
    neo4j_user = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password = os.getenv("NEO4J_PASSWORD", "ironask_dev_pw")

    # ---- LLM
    llm_provider = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
    ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    understand_model = os.getenv("LLM_UNDERSTAND_MODEL", "llama3.2:3b")
    answer_model = os.getenv("LLM_ANSWER_MODEL", "llama3.2:3b")
    llm_temperature = _f("LLM_TEMPERATURE", 0.0)
    llm_timeout_s = _i("LLM_TIMEOUT_S", 120)
    llm_cache = _b("LLM_CACHE", "1")
    llm_cache_dir = ROOT / os.getenv("LLM_CACHE_DIR", ".cache/llm")
    openai_api_key = os.getenv("OPENAI_API_KEY", "")
    openai_base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")

    # ---- Retrieval
    embed_model = os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5")
    rerank_model = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-base")
    retrieve_top_k = _i("RETRIEVE_TOP_K", 20)
    rrf_k = _i("RRF_K", 60)
    rerank_top_n = _i("RERANK_TOP_N", 5)
    rerank_threshold = _f("RERANK_THRESHOLD", 0.35)
    # How many fused candidates reach the cross-encoder, and how much of each
    # chunk it sees. Both are latency controls - see app/retrieval/focus.py.
    rerank_candidates = _i("RERANK_CANDIDATES", 10)
    rerank_max_tokens = _i("RERANK_MAX_TOKENS", 256)
    rerank_threads = _i("RERANK_THREADS", 8)

    # ---- Tracing
    langfuse_enabled = _b("LANGFUSE_ENABLED", "1")
    langfuse_host = os.getenv("LANGFUSE_HOST", "http://localhost:3100")
    langfuse_public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    langfuse_secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
    trace_file = ROOT / os.getenv("TRACE_FILE", "eval/results/traces.jsonl")

    # ---- App
    api_host = os.getenv("API_HOST", "0.0.0.0")
    api_port = _i("API_PORT", 8080)
    api_base_url = os.getenv("API_BASE_URL", "http://localhost:8080").rstrip("/")
    prompt_version = os.getenv("PROMPT_VERSION", "v1.3.0")
    index_dir = ROOT / os.getenv("INDEX_DIR", ".index")

    root = ROOT
    corpus_dir = ROOT / "corpus"

    # ---- Fixed client-facing messages (contract; do not reword) ----
    MSG_NOT_FOUND = "I couldn't find that in the documents and data you have access to."
    MSG_OUT_OF_SCOPE = "That's outside what this assistant covers."
    MSG_ERROR = "Something went wrong. Try again."

    def pg_dsn(self, user: str | None = None, password: str | None = None) -> str:
        u = user or self.pg_user
        p = password or self.pg_password
        return f"postgresql://{u}:{p}@{self.pg_host}:{self.pg_port}/{self.pg_db}"

    def today_ist(self) -> str:
        return datetime.now(IST).strftime("%Y-%m-%d")

    def now_ist(self) -> datetime:
        return datetime.now(IST)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.llm_cache_dir.mkdir(parents=True, exist_ok=True)
    s.index_dir.mkdir(parents=True, exist_ok=True)
    s.trace_file.parent.mkdir(parents=True, exist_ok=True)
    return s


settings = get_settings()

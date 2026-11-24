"""LLM client.

Default provider is Ollama running locally, which is what the budget and the
data terms both argue for: the prompt never leaves the machine, so there is no
question of it being used for training, and the marginal cost per question is
zero. OpenAI and Anthropic are wired as alternatives behind LLM_PROVIDER.

Two models are used, as the brief requires: a small one for the understand step
(llama3.2:3b) and a larger one for grounded generation (llama3.1:8b).

Every call is cached on disk in dev, keyed by provider+model+prompt+options, so
re-running the eval costs nothing and gives identical results.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field

import httpx

from app.config import settings


@dataclass
class LLMResult:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    cached: bool = False
    cost_inr: float = 0.0
    raw: dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# Per-1K-token prices in INR, used for the cost table. Ollama is local: zero.
PRICING_INR = {
    "ollama": (0.0, 0.0),
    "openai:gpt-4o-mini": (0.0125, 0.05),
    "openai:gpt-4o": (0.21, 0.84),
    "anthropic:claude-haiku-5-5": (0.021, 0.105),
}


class LLMError(RuntimeError):
    pass


def _cache_file(key: str):
    d = settings.llm_cache_dir
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


def _cache_key(provider: str, model: str, prompt: str, system: str, opts: dict) -> str:
    h = hashlib.sha256()
    for part in (provider, model, system, prompt, json.dumps(opts, sort_keys=True)):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


# --------------------------------------------------------------------- Ollama

def _ollama_generate(model: str, prompt: str, system: str, opts: dict) -> LLMResult:
    url = f"{settings.ollama_base_url}/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "system": system,
        "stream": False,
        "options": {
            "temperature": opts.get("temperature", settings.llm_temperature),
            "num_predict": opts.get("max_tokens", 1024),
            "top_p": opts.get("top_p", 0.9),
            # Deterministic by default so eval runs are comparable.
            "seed": opts.get("seed", 42),
        },
    }
    if opts.get("json_mode"):
        payload["format"] = "json"

    t0 = time.perf_counter()
    try:
        r = httpx.post(url, json=payload, timeout=settings.llm_timeout_s)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as exc:
        raise LLMError(f"ollama request failed: {exc}") from exc
    dt = (time.perf_counter() - t0) * 1000

    return LLMResult(
        text=data.get("response", ""),
        model=model,
        prompt_tokens=int(data.get("prompt_eval_count", 0)),
        completion_tokens=int(data.get("eval_count", 0)),
        latency_ms=dt,
        raw={k: v for k, v in data.items() if k != "response"},
    )


# --------------------------------------------------------------------- OpenAI

def _openai_generate(model: str, prompt: str, system: str, opts: dict) -> LLMResult:
    if not settings.openai_api_key:
        raise LLMError("LLM_PROVIDER=openai but OPENAI_API_KEY is empty")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": opts.get("temperature", settings.llm_temperature),
        "max_tokens": opts.get("max_tokens", 1024),
    }
    if opts.get("json_mode"):
        payload["response_format"] = {"type": "json_object"}

    t0 = time.perf_counter()
    try:
        r = httpx.post(
            f"{settings.openai_base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            timeout=settings.llm_timeout_s,
        )
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as exc:
        raise LLMError(f"openai request failed: {exc}") from exc
    dt = (time.perf_counter() - t0) * 1000

    usage = data.get("usage", {})
    return LLMResult(
        text=data["choices"][0]["message"]["content"],
        model=model,
        prompt_tokens=int(usage.get("prompt_tokens", 0)),
        completion_tokens=int(usage.get("completion_tokens", 0)),
        latency_ms=dt,
    )


# ------------------------------------------------------------------ Anthropic

def _anthropic_generate(model: str, prompt: str, system: str, opts: dict) -> LLMResult:
    if not settings.anthropic_api_key:
        raise LLMError("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is empty")
    t0 = time.perf_counter()
    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            json={
                "model": model,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": opts.get("max_tokens", 1024),
                "temperature": opts.get("temperature", settings.llm_temperature),
            },
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
            },
            timeout=settings.llm_timeout_s,
        )
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as exc:
        raise LLMError(f"anthropic request failed: {exc}") from exc
    dt = (time.perf_counter() - t0) * 1000

    usage = data.get("usage", {})
    return LLMResult(
        text="".join(b.get("text", "") for b in data.get("content", [])),
        model=model,
        prompt_tokens=int(usage.get("input_tokens", 0)),
        completion_tokens=int(usage.get("output_tokens", 0)),
        latency_ms=dt,
    )


def _price(provider: str, model: str, res: LLMResult) -> float:
    key = "ollama" if provider == "ollama" else f"{provider}:{model}"
    pin, pout = PRICING_INR.get(key, (0.0, 0.0))
    return (res.prompt_tokens / 1000.0) * pin + (res.completion_tokens / 1000.0) * pout


def generate(
    prompt: str,
    system: str = "",
    model: str | None = None,
    json_mode: bool = False,
    max_tokens: int = 1024,
    temperature: float | None = None,
    use_cache: bool | None = None,
) -> LLMResult:
    provider = settings.llm_provider
    model = model or settings.answer_model
    opts = {
        "json_mode": json_mode,
        "max_tokens": max_tokens,
        "temperature": settings.llm_temperature if temperature is None else temperature,
    }

    cache_on = settings.llm_cache if use_cache is None else use_cache
    key = _cache_key(provider, model, prompt, system, opts)
    if cache_on:
        f = _cache_file(key)
        if f.exists():
            try:
                blob = json.loads(f.read_text(encoding="utf-8"))
                return LLMResult(
                    text=blob["text"], model=blob["model"],
                    prompt_tokens=blob.get("prompt_tokens", 0),
                    completion_tokens=blob.get("completion_tokens", 0),
                    latency_ms=0.0, cached=True, cost_inr=0.0,
                )
            except (json.JSONDecodeError, KeyError):
                pass

    if provider == "ollama":
        res = _ollama_generate(model, prompt, system, opts)
    elif provider == "openai":
        res = _openai_generate(model, prompt, system, opts)
    elif provider == "anthropic":
        res = _anthropic_generate(model, prompt, system, opts)
    else:
        raise LLMError(f"unknown LLM_PROVIDER: {provider!r}")

    res.cost_inr = _price(provider, model, res)

    if cache_on:
        _cache_file(key).write_text(
            json.dumps({
                "text": res.text, "model": res.model,
                "prompt_tokens": res.prompt_tokens,
                "completion_tokens": res.completion_tokens,
            }),
            encoding="utf-8",
        )
    return res


# ------------------------------------------------------------- JSON extraction

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of a model response.

    Small local models wrap JSON in prose or fences even when told not to, so
    this tries the whole string, then a fenced block, then the first balanced
    {...} span, before giving up.
    """
    if not text:
        return None
    text = text.strip()

    for candidate in (text,):
        try:
            val = json.loads(candidate)
            if isinstance(val, dict):
                return val
        except json.JSONDecodeError:
            pass

    m = _FENCE.search(text)
    if m:
        try:
            val = json.loads(m.group(1).strip())
            if isinstance(val, dict):
                return val
        except json.JSONDecodeError:
            pass

    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    val = json.loads(text[start:i + 1])
                    if isinstance(val, dict):
                        return val
                except json.JSONDecodeError:
                    start = -1
    return None


def health() -> dict:
    """Is the configured provider reachable, and which models are present?"""
    if settings.llm_provider != "ollama":
        return {"provider": settings.llm_provider, "ok": bool(
            settings.openai_api_key or settings.anthropic_api_key
        )}
    try:
        r = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=5)
        r.raise_for_status()
        names = [m["name"] for m in r.json().get("models", [])]
        return {
            "provider": "ollama",
            "ok": True,
            "models": names,
            "understand_model_present": settings.understand_model in names,
            "answer_model_present": settings.answer_model in names,
        }
    except httpx.HTTPError as exc:
        return {"provider": "ollama", "ok": False, "error": str(exc)}

"""The only place that talks to Gemini. Agents call generate(); none import google-genai.

* Model name comes from settings (GEMINI_MODEL), so it can change without code edits.
* Responses are cached in Redis by a hash of (model, system, prompt), so re-running
  the demo does not spend free-tier quota.
* temperature=0: migration output should be as repeatable as possible.
* Retries rate-limit and transient server errors with exponential backoff.

Reliability (scope addition to step 7d, approved by Aarsh on 17 Nov):

* **Timeout.** Every request has a limit of ``GEMINI_TIMEOUT_SECONDS`` (default 120,
  10-600). The SDK sends the same value to Google as ``X-Server-Timeout``, so it is
  both our client limit and the server's deadline: a longer generation ends either in
  a server **504** (``ServerError``) or in a client timeout, re-raised here as
  ``LLMTimeout``. Neither is retried (``RETRYABLE_CODES``): with our own deadline a
  retry would hit the same limit and spend more free-tier quota.
* **Call records.** Each request (and each cache hit) produces one ``CallRecord``:
  model, try, elapsed time, outcome, timeout, prompt and response sizes, cache status.
  Never the prompt or response text, the key, or a URL. Records are logged on the
  ``pixelvyne.gemini`` logger, and collected only inside ``recording()``, which is
  scoped to its own block (and context), so one run's records cannot appear in
  another's report.
* **Cache.** Reading is strict: if Redis cannot be read, nothing is sent to Gemini
  (``llm_fallback`` reports ``cache_error``). Writing is tolerant: if Gemini answered
  but the answer cannot be stored, the answer is still returned (cache
  ``write_failed``), so a paid answer is never thrown away.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache

import redis
from google import genai
from google.genai import errors, types

from app.config import settings

CACHE_TTL_SECONDS = 7 * 24 * 3600
RETRYABLE_CODES = {429, 500, 503}   # not 504 and not client timeouts (Aarsh, 17 Nov)
MAX_TRIES = 4

log = logging.getLogger("pixelvyne.gemini")


class LLMNotConfigured(RuntimeError):
    pass


class LLMTimeout(RuntimeError):
    """The request ran past GEMINI_TIMEOUT_SECONDS on our side (no answer was received)."""


@dataclass(frozen=True)
class CallRecord:
    """One Gemini request, or one cache hit. Sizes only: never prompt or response text."""

    model: str
    attempt: int                 # 1..MAX_TRIES for a request; 0 for a cache hit (no request)
    elapsed_ms: int
    outcome: str                 # ok, cache_hit, http_<code>, client_timeout, error:<Type>
    timeout_s: int
    prompt_chars: int
    response_chars: int | None   # None when no answer was received
    cache: str                   # hit, stored, write_failed, not_stored, off


_RECORDS: ContextVar[list[CallRecord] | None] = ContextVar("pixelvyne_gemini_records", default=None)


@contextmanager
def recording() -> Iterator[list[CallRecord]]:
    """Collect the CallRecords of the calls made inside this block, and only those."""
    records: list[CallRecord] = []
    token = _RECORDS.set(records)
    try:
        yield records
    finally:
        _RECORDS.reset(token)


def _record(record: CallRecord) -> None:
    log.info(
        "gemini call model=%s try=%d elapsed_ms=%d outcome=%s timeout_s=%d prompt_chars=%d response_chars=%s cache=%s",
        record.model, record.attempt, record.elapsed_ms, record.outcome, record.timeout_s, record.prompt_chars,
        record.response_chars, record.cache,
    )
    records = _RECORDS.get()
    if records is not None:
        records.append(record)


def _is_client_timeout(exc: BaseException) -> bool:
    """httpx (or the SDK's httpx2) timeout, by class name so no transport is imported here."""
    return isinstance(exc, TimeoutError) or "TimeoutException" in {cls.__name__ for cls in type(exc).__mro__}


@lru_cache
def _client() -> genai.Client:
    if not settings.GEMINI_API_KEY:
        raise LLMNotConfigured("GEMINI_API_KEY is empty. Add it to .env and restart api/worker.")
    return genai.Client(
        api_key=settings.GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=settings.GEMINI_TIMEOUT_SECONDS * 1000),
    )


@lru_cache
def _cache() -> redis.Redis:
    return redis.Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )


def _cache_key(model: str, system: str | None, prompt: str) -> str:
    payload = json.dumps({"m": model, "s": system, "p": prompt}, sort_keys=True)
    return "llm:" + hashlib.sha256(payload.encode()).hexdigest()


def generate(prompt: str, *, system: str | None = None, use_cache: bool = True) -> str:
    model = settings.GEMINI_MODEL
    timeout_s = settings.GEMINI_TIMEOUT_SECONDS
    key = _cache_key(model, system, prompt)

    def since(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    def record(attempt: int, elapsed_ms: int, outcome: str, response_chars: int | None, cache: str) -> None:
        _record(CallRecord(model, attempt, elapsed_ms, outcome, timeout_s, len(prompt), response_chars, cache))

    if use_cache:
        cached = _cache().get(key)   # strict: a Redis error propagates and nothing is sent to Gemini
        if cached is not None:
            record(0, 0, "cache_hit", len(cached), "hit")
            return cached

    client = _client()
    config = types.GenerateContentConfig(temperature=0.0, system_instruction=system)
    not_stored = "not_stored" if use_cache else "off"
    for attempt in range(1, MAX_TRIES + 1):
        started = time.monotonic()
        try:
            response = client.models.generate_content(model=model, contents=prompt, config=config)
        except errors.APIError as exc:
            code = getattr(exc, "code", None)
            record(attempt, since(started), f"http_{code}" if isinstance(code, int) else f"error:{type(exc).__name__}",
                   None, not_stored)
            if code in RETRYABLE_CODES and attempt < MAX_TRIES:
                time.sleep(2 ** attempt)
                continue
            raise
        except Exception as exc:
            if _is_client_timeout(exc):
                record(attempt, since(started), "client_timeout", None, not_stored)
                raise LLMTimeout(f"client timeout after {timeout_s} s") from None
            record(attempt, since(started), f"error:{type(exc).__name__}", None, not_stored)
            raise
        elapsed_ms = since(started)   # the request only, not the cache write below
        break

    text = response.text or ""
    cache = not_stored
    if use_cache and text:
        try:
            _cache().set(key, text, ex=CACHE_TTL_SECONDS)
            cache = "stored"
        except redis.RedisError as exc:
            log.warning("Gemini answer not cached (%s); returned anyway", type(exc).__name__)
            cache = "write_failed"
    record(attempt, elapsed_ms, "ok", len(text), cache)
    return text

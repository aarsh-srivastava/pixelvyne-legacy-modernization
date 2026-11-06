"""The only place that talks to Gemini. Agents call generate(); none import google-genai.

* Model name comes from settings (GEMINI_MODEL), so it can change without code edits.
* Responses are cached in Redis by a hash of (model, system, prompt), so re-running
  the demo does not spend free-tier quota.
* temperature=0: migration output should be as repeatable as possible.
* Retries rate-limit and transient server errors with exponential backoff.
"""

from __future__ import annotations

import hashlib
import json
import time
from functools import lru_cache

import redis
from google import genai
from google.genai import errors, types

from app.config import settings

CACHE_TTL_SECONDS = 7 * 24 * 3600
RETRYABLE_CODES = {429, 500, 503}


class LLMNotConfigured(RuntimeError):
    pass


@lru_cache
def _client() -> genai.Client:
    if not settings.GEMINI_API_KEY:
        raise LLMNotConfigured("GEMINI_API_KEY is empty. Add it to .env and restart api/worker.")
    return genai.Client(api_key=settings.GEMINI_API_KEY)


@lru_cache
def _cache() -> redis.Redis:
    return redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)


def _cache_key(model: str, system: str | None, prompt: str) -> str:
    payload = json.dumps({"m": model, "s": system, "p": prompt}, sort_keys=True)
    return "llm:" + hashlib.sha256(payload.encode()).hexdigest()


def generate(prompt: str, *, system: str | None = None, use_cache: bool = True) -> str:
    model = settings.GEMINI_MODEL
    key = _cache_key(model, system, prompt)

    if use_cache:
        cached = _cache().get(key)
        if cached is not None:
            return cached

    config = types.GenerateContentConfig(temperature=0.0, system_instruction=system)
    for attempt in range(4):
        try:
            response = _client().models.generate_content(model=model, contents=prompt, config=config)
            break
        except errors.APIError as exc:
            if getattr(exc, "code", None) in RETRYABLE_CODES and attempt < 3:
                time.sleep(2 ** (attempt + 1))
                continue
            raise

    text = response.text or ""
    if use_cache and text:
        _cache().set(key, text, ex=CACHE_TTL_SECONDS)
    return text

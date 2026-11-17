"""llm_service call records, timeout reporting and cache behaviour (Aarsh 17 Nov, decisions 2 and 3).

No network: the Gemini client and Redis are fakes. Nothing here can spend quota.
"""

from __future__ import annotations

import logging

import httpx
import pytest
import redis
from google.genai import errors

from app.migration.llm_fallback import FallbackFailure, _classify
from app.services import llm_service

PROMPT = "convert this view SECRET-PROMPT-MARKER please"
ANSWER = '{"postgresql": "SECRET-ANSWER-MARKER"}'
KEY = "key-SECRET-KEY-MARKER"


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeModels:
    def __init__(self, script):
        self.script = list(script)   # each item: a text to answer, or an exception to raise
        self.calls = 0

    def generate_content(self, *, model, contents, config):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return FakeResponse(item)


class FakeClient:
    def __init__(self, script):
        self.models = FakeModels(script)


class FakeCache:
    def __init__(self, *, get_error=None, set_error=None, stored=None):
        self.get_error, self.set_error = get_error, set_error
        self.stored = dict(stored or {})

    def get(self, key):
        if self.get_error:
            raise self.get_error
        return self.stored.get(key)

    def set(self, key, value, ex=None):
        if self.set_error:
            raise self.set_error
        self.stored[key] = value


@pytest.fixture
def gemini(monkeypatch):
    """Install a fake client and cache; returns (client, cache) setters."""
    monkeypatch.setattr(llm_service.settings, "GEMINI_API_KEY", KEY)
    monkeypatch.setattr(llm_service.settings, "GEMINI_MODEL", "gemini-test")
    monkeypatch.setattr(llm_service.settings, "GEMINI_TIMEOUT_SECONDS", 120)
    monkeypatch.setattr(llm_service.time, "sleep", lambda s: None)
    state = {}

    def install(script, cache=None):
        state["client"] = FakeClient(script)
        state["cache"] = cache or FakeCache()
        monkeypatch.setattr(llm_service, "_client", lambda: state["client"])
        monkeypatch.setattr(llm_service, "_cache", lambda: state["cache"])
        return state["client"], state["cache"]

    return install


def server_error(code):
    return errors.ServerError(code, {"error": {"code": code, "message": "x", "status": "DEADLINE_EXCEEDED"}})


def client_error(code):
    return errors.ClientError(code, {"error": {"code": code, "message": "x", "status": "RESOURCE_EXHAUSTED"}})


# --- one record per call, scoped to its block -------------------------------------------------

def test_a_successful_call_gives_one_ok_record_with_sizes_only(gemini):
    client, cache = gemini([ANSWER])
    with llm_service.recording() as records:
        assert llm_service.generate(PROMPT) == ANSWER
    [r] = records
    assert (r.model, r.attempt, r.outcome, r.timeout_s, r.cache) == ("gemini-test", 1, "ok", 120, "stored")
    assert (r.prompt_chars, r.response_chars) == (len(PROMPT), len(ANSWER)) and r.elapsed_ms >= 0
    assert client.models.calls == 1


def test_a_cache_hit_is_recorded_and_sends_nothing(gemini):
    key = llm_service._cache_key("gemini-test", None, PROMPT)
    client, _ = gemini([], cache=FakeCache(stored={key: ANSWER}))
    with llm_service.recording() as records:
        assert llm_service.generate(PROMPT) == ANSWER
    assert [(r.attempt, r.outcome, r.cache) for r in records] == [(0, "cache_hit", "hit")]
    assert client.models.calls == 0


def test_records_belong_to_their_own_block_only(gemini):
    gemini([ANSWER, ANSWER])
    with llm_service.recording() as first:
        llm_service.generate(PROMPT + " one")
    with llm_service.recording() as second:
        llm_service.generate(PROMPT + " two")
    assert len(first) == 1 and len(second) == 1 and first[0] is not second[0]
    llm_service.generate(PROMPT + " one")   # outside any block: a cache hit, collected nowhere
    assert len(first) == 1 and len(second) == 1


def test_records_never_hold_prompt_answer_key_or_url(gemini, caplog):
    gemini([server_error(504)])
    caplog.set_level(logging.INFO, logger="pixelvyne.gemini")
    with llm_service.recording() as records, pytest.raises(errors.ServerError):
        llm_service.generate(PROMPT)
    gemini([ANSWER])
    with llm_service.recording() as more:
        llm_service.generate(PROMPT + "x")
    text = repr(records + more) + caplog.text
    for secret in ("SECRET-PROMPT-MARKER", "SECRET-ANSWER-MARKER", "SECRET-KEY-MARKER", "redis://", "http"):
        assert secret not in text.replace("http_504", "")
    assert "outcome=http_504" in caplog.text and "outcome=ok" in caplog.text


# --- decision 2: 504 and client timeouts are recorded apart and never retried -------------------

def test_a_server_504_is_one_try_recorded_as_http_504(gemini):
    client, _ = gemini([server_error(504), ANSWER])
    with llm_service.recording() as records, pytest.raises(errors.ServerError):
        llm_service.generate(PROMPT)
    assert client.models.calls == 1
    assert [(r.attempt, r.outcome, r.response_chars, r.cache) for r in records] == [(1, "http_504", None, "not_stored")]


@pytest.mark.parametrize("timeout", [httpx.ReadTimeout("read"), httpx.ConnectTimeout("connect"), TimeoutError()])
def test_a_client_timeout_is_one_try_raised_as_llm_timeout(gemini, timeout):
    client, _ = gemini([timeout, ANSWER])
    with llm_service.recording() as records, pytest.raises(llm_service.LLMTimeout, match="after 120 s"):
        llm_service.generate(PROMPT)
    assert client.models.calls == 1
    assert [(r.attempt, r.outcome) for r in records] == [(1, "client_timeout")]


def test_the_two_failures_read_differently_in_the_run_report():
    # llm_fallback reports f"{type(exc).__name__}" + f" {exc.code}" when the code is an int:
    # "Gemini call failed (LLMTimeout)" vs "Gemini call failed (ServerError 504)", both api_error.
    assert _classify(llm_service.LLMTimeout("client timeout after 120 s")) == FallbackFailure.API_ERROR
    assert type(llm_service.LLMTimeout("x")).__name__ == "LLMTimeout"
    assert _classify(server_error(504)) == FallbackFailure.API_ERROR
    assert getattr(server_error(504), "code", None) == 504   # llm_fallback reports "ServerError 504"


@pytest.mark.parametrize("first", [client_error(429), server_error(500), server_error(503)])
def test_429_500_503_are_still_retried_and_each_try_is_recorded(gemini, first):
    client, _ = gemini([first, ANSWER])
    with llm_service.recording() as records:
        assert llm_service.generate(PROMPT) == ANSWER
    assert client.models.calls == 2
    assert [(r.attempt, r.outcome) for r in records] == [(1, f"http_{first.code}"), (2, "ok")]


def test_the_retry_codes_are_exactly_429_500_503():
    assert llm_service.RETRYABLE_CODES == {429, 500, 503}


def test_retries_stop_after_four_tries(gemini):
    client, _ = gemini([server_error(503)] * 5)
    with llm_service.recording() as records, pytest.raises(errors.ServerError):
        llm_service.generate(PROMPT)
    assert client.models.calls == 4 and [r.attempt for r in records] == [1, 2, 3, 4]


def test_another_error_is_recorded_by_type_and_raised(gemini):
    gemini([httpx.ConnectError("refused")])
    with llm_service.recording() as records, pytest.raises(httpx.ConnectError):
        llm_service.generate(PROMPT)
    assert [r.outcome for r in records] == ["error:ConnectError"]


# --- decision 3: strict cache read, tolerant cache write ---------------------------------------

def test_a_failed_cache_write_still_returns_the_answer(gemini, caplog):
    gemini([ANSWER], cache=FakeCache(set_error=redis.TimeoutError("slow")))
    caplog.set_level(logging.WARNING, logger="pixelvyne.gemini")
    with llm_service.recording() as records:
        assert llm_service.generate(PROMPT) == ANSWER
    assert [(r.outcome, r.cache) for r in records] == [("ok", "write_failed")]
    assert "not cached (TimeoutError); returned anyway" in caplog.text


@pytest.mark.parametrize("error", [redis.ConnectionError("down"), redis.TimeoutError("slow")])
def test_a_failed_cache_read_raises_and_gemini_is_never_called(gemini, error):
    client, _ = gemini([ANSWER], cache=FakeCache(get_error=error))
    with llm_service.recording() as records, pytest.raises(redis.RedisError):
        llm_service.generate(PROMPT)
    assert client.models.calls == 0 and records == []
    assert _classify(error) == FallbackFailure.CACHE_ERROR


def test_without_the_cache_nothing_is_read_or_stored(gemini):
    _, cache = gemini([ANSWER], cache=FakeCache(get_error=redis.ConnectionError("unused")))
    with llm_service.recording() as records:
        assert llm_service.generate(PROMPT, use_cache=False) == ANSWER
    assert [r.cache for r in records] == ["off"] and cache.stored == {}


def test_an_empty_answer_is_returned_and_not_stored(gemini):
    _, cache = gemini([""])
    with llm_service.recording() as records:
        assert llm_service.generate(PROMPT) == ""
    assert [(r.outcome, r.response_chars, r.cache) for r in records] == [("ok", 0, "not_stored")]
    assert cache.stored == {}


def test_not_configured_is_raised_before_any_try(monkeypatch):
    monkeypatch.setattr(llm_service.settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(llm_service, "_cache", lambda: FakeCache())
    llm_service._client.cache_clear()
    try:
        with llm_service.recording() as records, pytest.raises(llm_service.LLMNotConfigured):
            llm_service.generate(PROMPT)
    finally:
        llm_service._client.cache_clear()
    assert records == []

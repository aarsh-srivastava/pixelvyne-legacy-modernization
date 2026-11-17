"""Gemini and Redis timeouts in llm_service (reliability scope addition, Aarsh 17 Nov).

The Gemini timeout comes from GEMINI_TIMEOUT_SECONDS (default 120, 10-600). It replaces the
fixed 30 s that bd4a7f9 introduced, on purpose (approved decision 1), not as a weakening.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services import llm_service


def _client_options(monkeypatch, timeout_seconds):
    llm_service._client.cache_clear()
    captured = {}

    class FakeClient:
        pass

    def fake_client(**kwargs):
        captured.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(llm_service.settings, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm_service.settings, "GEMINI_TIMEOUT_SECONDS", timeout_seconds)
    monkeypatch.setattr(llm_service.genai, "Client", fake_client)
    try:
        llm_service._client()
    finally:
        llm_service._client.cache_clear()
    return captured["http_options"]


def test_the_default_gemini_timeout_is_120_seconds():
    assert Settings.model_fields["GEMINI_TIMEOUT_SECONDS"].default == 120
    assert Settings(_env_file=None).GEMINI_TIMEOUT_SECONDS == 120


@pytest.mark.parametrize("seconds", [120, 45, 10, 600])
def test_the_gemini_client_uses_the_configured_timeout(monkeypatch, seconds):
    assert _client_options(monkeypatch, seconds).timeout == seconds * 1000   # the SDK takes milliseconds


@pytest.mark.parametrize("seconds", [9, 0, -1, 601, 3600])
def test_a_timeout_outside_10_to_600_seconds_is_refused_at_start_up(seconds):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, GEMINI_TIMEOUT_SECONDS=seconds)


def test_the_timeout_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "90")
    assert Settings(_env_file=None).GEMINI_TIMEOUT_SECONDS == 90
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "5")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_the_timeout_is_also_googles_server_deadline():
    """The SDK sends the timeout as X-Server-Timeout (why 504s and client timeouts both mean 'too slow')."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key="not-used", http_options=types.HttpOptions(timeout=120_000))
    build = getattr(client._api_client, "_build_request", None)   # SDK internals: no request is sent
    if build is None:
        pytest.skip("this google-genai version builds requests differently")
    request = build("post", "models/m:generateContent", {"contents": []})
    assert request.headers.get("X-Server-Timeout") == "120"
    assert request.timeout == 120.0


def test_redis_client_has_connection_and_socket_timeouts(monkeypatch):
    llm_service._cache.cache_clear()
    captured = {}

    class FakeRedis:
        @classmethod
        def from_url(cls, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return cls()

    monkeypatch.setattr(llm_service.redis, "Redis", FakeRedis)

    try:
        llm_service._cache()
    finally:
        llm_service._cache.cache_clear()

    assert captured["socket_connect_timeout"] == 5
    assert captured["socket_timeout"] == 5
    assert captured["decode_responses"] is True

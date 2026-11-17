from app.services import llm_service


def test_gemini_client_has_30_second_timeout(monkeypatch):
    llm_service._client.cache_clear()
    captured = {}

    class FakeClient:
        pass

    def fake_client(**kwargs):
        captured.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(llm_service.settings, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm_service.genai, "Client", fake_client)

    try:
        llm_service._client()
    finally:
        llm_service._client.cache_clear()

    assert captured["http_options"].timeout == 30_000


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

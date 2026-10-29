"""Optional Redis, Cosmos, and Azure Language integration tests."""

import pytest

from app.integrations import (
    AzureLanguagePrivacyClient,
    CosmosDecisionStore,
    RedisRateLimiter,
)


class _FakePipeline:
    def __init__(self, client):
        self.client = client
        self.key = None

    def incr(self, key):
        self.key = key
        return self

    def expire(self, key, ttl):
        return self

    def execute(self):
        if self.client.fail:
            raise RuntimeError("redis down")
        self.client.counts[self.key] = self.client.counts.get(self.key, 0) + 1
        return [self.client.counts[self.key], True]


class _FakeRedis:
    def __init__(self, fail=False):
        self.counts = {}
        self.fail = fail

    def pipeline(self):
        return _FakePipeline(self)


class _NotFound(Exception):
    status_code = 404


class _ServiceError(Exception):
    status_code = 500


class _FakeContainer:
    def __init__(self):
        self.items = {}
        self.fail_get = None
        self.fail_save = False

    def read_item(self, item, partition_key):
        if self.fail_get is not None:
            raise self.fail_get
        if item not in self.items:
            raise _NotFound()
        return self.items[item]

    def upsert_item(self, payload):
        if self.fail_save:
            raise RuntimeError("write failed")
        self.items[payload["id"]] = payload
        return payload


def test_redis_rate_limiter_enforces_fixed_window_limit():
    limiter = RedisRateLimiter(
        client=_FakeRedis(), rate_limit=2, window_seconds=60
    )
    assert limiter.enabled is True
    assert limiter.acquire("intent_classifier") is True
    assert limiter.acquire("intent_classifier") is True
    assert limiter.acquire("intent_classifier") is False


def test_redis_rate_limiter_fails_closed_on_errors():
    limiter = RedisRateLimiter(
        client=_FakeRedis(fail=True), rate_limit=10, window_seconds=60
    )
    assert limiter.acquire("evaluator") is False


def test_redis_rate_limiter_without_url_is_noop(monkeypatch):
    monkeypatch.setattr("app.integrations.REDIS_URL", "")
    limiter = RedisRateLimiter(client=None, redis_url="")
    assert limiter.enabled is False
    assert limiter.acquire("any") is True


def test_cosmos_store_reads_and_saves_with_partition_key():
    container = _FakeContainer()
    store = CosmosDecisionStore(container=container)

    assert store.enabled is True
    assert store.get("abc") is None

    store.save("abc", {"status": "READY_FOR_REVIEW"})
    saved = store.get("abc")
    assert saved["id"] == "abc"
    assert saved["partitionKey"] == "adjustment"
    assert saved["status"] == "READY_FOR_REVIEW"


def test_cosmos_store_fails_closed_on_non_404_read_error():
    container = _FakeContainer()
    container.fail_get = _ServiceError()
    store = CosmosDecisionStore(container=container)
    with pytest.raises(RuntimeError, match="durable idempotency"):
        store.get("abc")


def test_cosmos_store_fails_closed_on_write_error():
    container = _FakeContainer()
    container.fail_save = True
    store = CosmosDecisionStore(container=container)
    with pytest.raises(RuntimeError, match="persist"):
        store.save("abc", {"status": "READY_FOR_REVIEW"})


class _AzureResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


def test_azure_language_redacts_entities_by_offset():
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return _AzureResponse(
            body={
                "results": {
                    "documents": [
                        {
                            "id": "1",
                            "entities": [
                                {"offset": 9, "length": 8, "category": "Person"},
                                {"offset": 24, "length": 10, "category": "Email"},
                            ],
                            "redactedText": "unused",
                        }
                    ]
                }
            }
        )

    client = AzureLanguagePrivacyClient(
        endpoint="https://example.cognitiveservices.azure.com/",
        api_key="test-key",
        post=post,
    )
    text = "Employee Jane Doe email jane@x.com"
    redacted, counts = client.redact_text(text)

    assert redacted == "Employee [REDACTED_PERSON] email [REDACTED_EMAIL]"
    assert counts == {"PERSON": 1, "EMAIL": 1}
    assert calls[0][0].endswith("/language/:analyze-text")
    assert calls[0][1]["headers"]["Ocp-Apim-Subscription-Key"] == "test-key"
    assert calls[0][1]["json"]["kind"] == "PiiEntityRecognition"


def test_azure_language_http_error_falls_back():
    client = AzureLanguagePrivacyClient(
        endpoint="https://example.cognitiveservices.azure.com",
        api_key="test-key",
        post=lambda *args, **kwargs: _AzureResponse(status_code=429),
    )
    assert client.redact_text("Jane Doe") is None


def test_azure_language_disabled_without_configuration():
    client = AzureLanguagePrivacyClient(endpoint="", api_key="")
    assert client.enabled is False
    assert client.redact_text("Jane Doe") is None

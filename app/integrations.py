"""Optional shared Redis throttling and Cosmos audit/idempotency storage.

Cloud SDKs are imported lazily so offline/local runs need no extra packages.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from app.config import (
    AZURE_LANGUAGE_API_VERSION, AZURE_LANGUAGE_ENDPOINT, AZURE_LANGUAGE_KEY,
    AZURE_LANGUAGE_MODEL_VERSION, AZURE_LANGUAGE_TIMEOUT_SECONDS,
    COSMOS_CONTAINER, COSMOS_DATABASE, COSMOS_ENDPOINT, COSMOS_KEY,
    REDIS_RATE_LIMIT, REDIS_RATE_WINDOW_SECONDS, REDIS_URL,
)

logger = logging.getLogger("manual_adjustments.integrations")


class RedisRateLimiter:
    """Fixed-window distributed limiter. No-op when REDIS_URL is unset."""

    def __init__(
        self,
        client: Any = None,
        redis_url: str | None = None,
        rate_limit: int | None = None,
        window_seconds: int | None = None,
    ) -> None:
        self.rate_limit = max(1, int(rate_limit if rate_limit is not None else REDIS_RATE_LIMIT))
        self.window_seconds = max(
            1, int(window_seconds if window_seconds is not None else REDIS_RATE_WINDOW_SECONDS)
        )
        self.url = REDIS_URL if redis_url is None else redis_url
        self.client = client
        if self.client is None and self.url:
            try:
                import redis

                self.client = redis.Redis.from_url(
                    self.url,
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                self.client.ping()
            except Exception as exc:
                logger.error("Redis rate limiter unavailable: %s", type(exc).__name__)
                raise RuntimeError("Redis is configured but unavailable") from exc

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def acquire(self, bucket: str = "llm") -> bool:
        if self.client is None:
            return True
        window = int(time.time()) // self.window_seconds
        safe_bucket = "".join(ch for ch in str(bucket or "llm") if ch.isalnum() or ch in "-_") or "llm"
        key = f"manual-adjustments:rate:{safe_bucket}:{window}"
        try:
            pipe = self.client.pipeline()
            pipe.incr(key)
            pipe.expire(key, self.window_seconds + 2)
            count, _ = pipe.execute()
            allowed = int(count) <= self.rate_limit
            if not allowed:
                logger.warning("Redis rate limit exceeded for bucket '%s'", safe_bucket)
            return allowed
        except Exception as exc:
            logger.error("Redis rate-limit check failed: %s", type(exc).__name__)
            return False

    def close(self) -> None:
        if self.client is not None and hasattr(self.client, "close"):
            try:
                self.client.close()
            except Exception:
                logger.debug("Redis client close failed", exc_info=True)


class CosmosDecisionStore:
    """Optional durable idempotency and decision storage."""

    def __init__(self, container: Any = None) -> None:
        self.container = container
        if self.container is None and COSMOS_ENDPOINT and COSMOS_KEY:
            try:
                from azure.cosmos import CosmosClient, PartitionKey

                client = CosmosClient(COSMOS_ENDPOINT, credential=COSMOS_KEY)
                db = client.create_database_if_not_exists(id=COSMOS_DATABASE)
                self.container = db.create_container_if_not_exists(
                    id=COSMOS_CONTAINER, partition_key=PartitionKey(path="/partitionKey")
                )
            except Exception as exc:
                logger.exception("Cosmos initialization failed")
                raise RuntimeError("Cosmos is configured but unavailable") from exc

    @property
    def enabled(self) -> bool:
        return self.container is not None

    def get(self, idempotency_key: str) -> dict[str, Any] | None:
        if not self.container:
            return None
        try:
            return self.container.read_item(item=idempotency_key, partition_key="adjustment")
        except Exception as exc:
            # Cosmos 404 means no prior execution; other errors must fail closed.
            if getattr(exc, "status_code", None) == 404:
                return None
            logger.exception("Cosmos idempotency lookup failed")
            raise RuntimeError("Could not verify durable idempotency") from exc

    def save(self, idempotency_key: str, document: dict[str, Any]) -> None:
        if not self.container:
            return
        payload = dict(document)
        payload.update({"id": idempotency_key, "partitionKey": "adjustment"})
        try:
            self.container.upsert_item(payload)
        except Exception as exc:
            logger.exception("Cosmos decision persistence failed")
            raise RuntimeError("Could not persist adjustment decision") from exc


class AzureLanguagePrivacyClient:
    """Azure AI Language PII redaction through the supported REST endpoint.

    The optional Azure SDK is deliberately not required. If configuration is
    absent or the service is unavailable, callers fall back to local redaction.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        api_key: str | None = None,
        api_version: str | None = None,
        timeout: float | None = None,
        model_version: str | None = None,
        post: Any = None,
    ) -> None:
        self.endpoint = (AZURE_LANGUAGE_ENDPOINT if endpoint is None else endpoint).strip()
        self.api_key = (AZURE_LANGUAGE_KEY if api_key is None else api_key).strip()
        self.api_version = (
            AZURE_LANGUAGE_API_VERSION if api_version is None else api_version
        ).strip()
        self.timeout = float(
            AZURE_LANGUAGE_TIMEOUT_SECONDS if timeout is None else timeout
        )
        self.model_version = (
            AZURE_LANGUAGE_MODEL_VERSION if model_version is None else model_version
        ).strip() or "latest"
        self._post = post

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint and self.api_key)

    def redact_text(
        self, text: str, language: str = "en"
    ) -> tuple[str, dict[str, int]] | None:
        if not self.enabled or not text:
            return None

        payload = {
            "kind": "PiiEntityRecognition",
            "parameters": {
                "modelVersion": self.model_version,
                "stringIndexType": "UnicodeCodePoint",
            },
            "analysisInput": {
                "documents": [
                    {"id": "1", "language": language or "en", "text": text}
                ]
            },
        }
        url = f"{self.endpoint.rstrip('/')}/language/:analyze-text"
        try:
            if self._post is None:
                import requests

                response = requests.post(
                    url,
                    params={"api-version": self.api_version},
                    headers={
                        "Ocp-Apim-Subscription-Key": self.api_key,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.timeout,
                )
            else:
                response = self._post(
                    url,
                    params={"api-version": self.api_version},
                    headers={"Ocp-Apim-Subscription-Key": self.api_key},
                    json=payload,
                    timeout=self.timeout,
                )

            if getattr(response, "status_code", 0) >= 400:
                logger.warning(
                    "Azure Language PII request failed with HTTP %s",
                    getattr(response, "status_code", "unknown"),
                )
                return None

            body = response.json()
            documents = (
                (body.get("results") or {}).get("documents")
                if isinstance(body, dict)
                else None
            )
            if not documents:
                logger.warning("Azure Language PII response contained no documents")
                return None

            document = documents[0]
            entities = document.get("entities") or []
            redacted = document.get("redactedText")
            counts: dict[str, int] = {}

            if entities:
                redacted, counts = self._apply_entities(text, entities)
            if not isinstance(redacted, str):
                logger.warning("Azure Language PII response contained no redacted text")
                return None
            return redacted, counts
        except Exception as exc:
            logger.warning(
                "Azure Language PII request failed: %s", type(exc).__name__
            )
            return None

    @staticmethod
    def _apply_entities(
        text: str, entities: list[dict[str, Any]]
    ) -> tuple[str, dict[str, int]]:
        ranges: list[tuple[int, int, str]] = []
        counts: dict[str, int] = {}
        for entity in entities:
            try:
                offset = int(entity.get("offset"))
                length = int(entity.get("length"))
            except (TypeError, ValueError):
                continue
            if offset < 0 or length <= 0 or offset + length > len(text):
                continue
            category = str(entity.get("category") or "PII").upper()
            ranges.append((offset, offset + length, category))
            counts[category] = counts.get(category, 0) + 1

        if not ranges:
            return text, counts

        merged: list[tuple[int, int, str]] = []
        for start, end, category in sorted(ranges):
            if merged and start <= merged[-1][1]:
                prev_start, prev_end, prev_category = merged[-1]
                merged[-1] = (
                    prev_start,
                    max(prev_end, end),
                    prev_category if prev_category == category else "PII",
                )
            else:
                merged.append((start, end, category))

        parts: list[str] = []
        cursor = 0
        for start, end, category in merged:
            parts.append(text[cursor:start])
            parts.append(f"[REDACTED_{category}]")
            cursor = end
        parts.append(text[cursor:])
        return "".join(parts), counts


def stable_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

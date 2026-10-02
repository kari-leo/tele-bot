"""AliBailian OpenAI-compatible embedding client with bounded retries and batching."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import httpx

from tele_bot.config.llm.settings import AliBailianSettings


class EmbeddingError(RuntimeError):
    pass


class EmbeddingAuthenticationError(EmbeddingError):
    pass


class EmbeddingRateLimitError(EmbeddingError):
    pass


class EmbeddingTransientError(EmbeddingError):
    pass


@dataclass(frozen=True)
class EmbeddingBatchResult:
    vectors: tuple[tuple[float, ...], ...]
    input_tokens: int
    request_id: str | None
    model: str


class AliBailianEmbeddingClient:
    def __init__(
        self,
        settings: AliBailianSettings,
        *,
        post: Callable[..., httpx.Response] | None = None,
        max_retries: int = 2,
        max_input_chars: int = 24_000,
    ) -> None:
        self.settings = settings
        self._post = post or httpx.post
        self.max_retries = max_retries
        self.max_input_chars = max_input_chars

    def embed(self, texts: Sequence[str]) -> list[EmbeddingBatchResult]:
        if not texts:
            return []
        normalized = [text.strip() for text in texts]
        if any(not text for text in normalized):
            raise ValueError("embedding input cannot be empty")
        if any(len(text) > self.max_input_chars for text in normalized):
            raise ValueError(f"embedding input exceeds {self.max_input_chars} characters")
        return [
            self._request(normalized[start : start + self.settings.embedding_batch_size])
            for start in range(0, len(normalized), self.settings.embedding_batch_size)
        ]

    def _request(self, texts: Sequence[str]) -> EmbeddingBatchResult:
        api_key = self.settings.require_embedding_credentials()
        url = f"{self.settings.embedding_base_url.rstrip('/')}/embeddings"
        for attempt in range(self.max_retries + 1):
            try:
                response = self._post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": self.settings.embedding_model,
                        "input": list(texts),
                        "dimensions": self.settings.embedding_dimensions,
                        "encoding_format": "float",
                    },
                    timeout=self.settings.embedding_timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt >= self.max_retries:
                    raise EmbeddingTransientError(f"embedding network failure: {exc}") from exc
                time.sleep(0.25 * (2**attempt))
                continue
            if response.status_code in {408, 429} or response.status_code >= 500:
                if attempt < self.max_retries:
                    time.sleep(0.25 * (2**attempt))
                    continue
            return self._parse_response(response, len(texts))
        raise AssertionError("unreachable")

    def _parse_response(self, response: httpx.Response, expected_count: int) -> EmbeddingBatchResult:
        if response.status_code in {401, 403}:
            raise EmbeddingAuthenticationError("embedding credentials were rejected")
        if response.status_code == 429:
            raise EmbeddingRateLimitError("embedding service rate limit exceeded")
        if response.status_code in {408} or response.status_code >= 500:
            raise EmbeddingTransientError(f"embedding service returned HTTP {response.status_code}")
        if not response.is_success:
            raise EmbeddingError(f"embedding service returned HTTP {response.status_code}: {response.text[:500]}")
        try:
            payload: dict[str, Any] = response.json()
            ordered = sorted(payload.get("data", []), key=lambda item: item.get("index", 0))
            vectors = tuple(tuple(float(value) for value in item["embedding"]) for item in ordered)
        except (ValueError, TypeError, KeyError) as exc:
            raise EmbeddingError("embedding response has an invalid payload") from exc
        if len(vectors) != expected_count:
            raise EmbeddingError("embedding response count does not match request")
        if any(len(vector) != self.settings.embedding_dimensions for vector in vectors):
            raise EmbeddingError(
                f"embedding response dimension is not {self.settings.embedding_dimensions}"
            )
        usage = payload.get("usage", {})
        tokens = int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0)
        request_id = (
            response.headers.get("x-request-id")
            or payload.get("request_id")
            or payload.get("id")
        )
        return EmbeddingBatchResult(
            vectors=vectors,
            input_tokens=tokens,
            request_id=str(request_id) if request_id else None,
            model=str(payload.get("model") or self.settings.embedding_model),
        )

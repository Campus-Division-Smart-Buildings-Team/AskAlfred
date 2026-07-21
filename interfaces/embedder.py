# Embedder port
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Optional, Protocol

from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    NotFoundError,
    OpenAI,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)

from config import (
    INGEST_EMBED_BATCH_SIZE,
    INGEST_EMBED_MAX_RETRIES,
    INGEST_RETRY_EXP_MAX,
    INGEST_RETRY_EXP_MIN,
    INGEST_RETRY_EXP_MULTIPLIER,
)
from core.clients import get_oai


class Embedder(Protocol):
    def embed_texts(
        self,
        texts: list[str],
        *,
        model: str,
        timeout: Optional[float] = None,
        max_batch: Optional[int] = None,
    ) -> "EmbeddingsResult": ...


@dataclass(frozen=True)
class EmbeddingsResult:
    embeddings_by_index: dict[int, list[float]]
    errors_by_index: dict[int, str]
    retry_attempts: int = 0
    batch_reductions: int = 0
    rate_limit_errors: int = 0
    error_summary: str | None = None
    fatal_error: bool = False
    # VECTOR-04: number of one-shot safe retries issued because a provider
    # response length differed from the request length, and the number of
    # batches that still mismatched after that retry (a provider contract
    # breach the ingestion boundary alerts on).
    response_mismatch_retries: int = 0
    response_mismatch_batches: int = 0


class OpenAIEmbedder:
    """OpenAI embeddings with retry, backoff, and batch splitting."""

    def __init__(self, client: Optional[OpenAI] = None):
        self._client = client or get_oai()

    def _sleep_backoff(self, attempt: int) -> None:
        # Exponential backoff with jitter.
        exp = min(
            INGEST_RETRY_EXP_MAX,
            INGEST_RETRY_EXP_MIN * (INGEST_RETRY_EXP_MULTIPLIER**attempt),
        )
        base = min(INGEST_RETRY_EXP_MAX, exp)
        jitter = random.random()
        time.sleep(min(INGEST_RETRY_EXP_MAX, base + jitter))

    def _embed_batch(
        self,
        texts: list[str],
        *,
        model: str,
        timeout: Optional[float],
    ) -> tuple[list[list[float]] | None, int, int, str | None, bool]:
        retry_attempts = 0
        rate_limit_errors = 0
        for attempt in range(INGEST_EMBED_MAX_RETRIES):
            try:
                res = self._client.embeddings.create(
                    model=model,
                    input=texts,
                    timeout=timeout,
                )
                return (
                    [d.embedding for d in res.data],
                    retry_attempts,
                    rate_limit_errors,
                    None,
                    False,
                )
            except RateLimitError:
                rate_limit_errors += 1
                if attempt >= INGEST_EMBED_MAX_RETRIES - 1:
                    return None, retry_attempts, rate_limit_errors, "rate_limit", False
                retry_attempts += 1
                self._sleep_backoff(attempt)
            except (APIConnectionError, APITimeoutError):
                if attempt >= INGEST_EMBED_MAX_RETRIES - 1:
                    return (
                        None,
                        retry_attempts,
                        rate_limit_errors,
                        "network_error",
                        False,
                    )
                retry_attempts += 1
                self._sleep_backoff(attempt)
            except (AuthenticationError, PermissionDeniedError):
                return None, retry_attempts, rate_limit_errors, "auth_error", True
            except (BadRequestError, UnprocessableEntityError, ConflictError):
                return (
                    None,
                    retry_attempts,
                    rate_limit_errors,
                    "invalid_request",
                    False,
                )
            except NotFoundError:
                return None, retry_attempts, rate_limit_errors, "not_found", True
            except APIError:
                if attempt >= INGEST_EMBED_MAX_RETRIES - 1:
                    return None, retry_attempts, rate_limit_errors, "api_error", False
                retry_attempts += 1
                self._sleep_backoff(attempt)
            except Exception:
                return (
                    None,
                    retry_attempts,
                    rate_limit_errors,
                    "unexpected_error",
                    False,
                )
        return None, retry_attempts, rate_limit_errors, "failed_after_retries", False

    def embed_texts(
        self,
        texts: list[str],
        *,
        model: str,
        timeout: Optional[float] = None,
        max_batch: Optional[int] = None,
    ) -> EmbeddingsResult:
        if not texts:
            return EmbeddingsResult(
                embeddings_by_index={},
                errors_by_index={},
                error_summary=None,
            )
        initial_batch_size = max_batch or INGEST_EMBED_BATCH_SIZE
        if initial_batch_size <= 0:
            initial_batch_size = len(texts)

        embeddings_by_index: dict[int, list[float]] = {}
        errors_by_index: dict[int, str] = {}
        errors: list[str] = []
        retry_attempts = 0
        batch_reductions = 0
        rate_limit_errors = 0
        response_mismatch_retries = 0
        response_mismatch_batches = 0
        terminal_fatal_error = False
        i = 0
        batch_size = initial_batch_size
        while i < len(texts):
            batch = texts[i : i + batch_size]
            result, retries_used, rate_limit_used, error_reason, fatal_error = (
                self._embed_batch(batch, model=model, timeout=timeout)
            )
            retry_attempts += retries_used
            rate_limit_errors += rate_limit_used
            if result is not None and len(result) == len(batch):
                for offset, embedding in enumerate(result):
                    embeddings_by_index[i + offset] = embedding
                i += batch_size
                batch_size = initial_batch_size
                continue
            if result is not None and len(result) != len(batch):
                # VECTOR-04: a response whose length differs from the request is
                # a provider contract breach. Re-issuing an embeddings call has
                # no side effects, so retry the same batch once before giving up;
                # a transient mismatch usually clears on the retry.
                response_mismatch_retries += 1
                retry_result, retries_used, rate_limit_used, _reason, _fatal = (
                    self._embed_batch(batch, model=model, timeout=timeout)
                )
                retry_attempts += retries_used
                rate_limit_errors += rate_limit_used
                if retry_result is not None and len(retry_result) == len(batch):
                    for offset, embedding in enumerate(retry_result):
                        embeddings_by_index[i + offset] = embedding
                    i += batch_size
                    batch_size = initial_batch_size
                    continue
                # Still mismatched after one safe retry: record the contract
                # failure per item so the file finishes partial/failed, and flag
                # the batch so the ingestion boundary can alert operators.
                response_mismatch_batches += 1
                for offset in range(len(batch)):
                    errors_by_index[i + offset] = "response_size_mismatch"
                errors.append("response_size_mismatch")
                i += batch_size
                batch_size = initial_batch_size
                continue

            if fatal_error and error_reason:
                terminal_fatal_error = True
                for offset in range(len(batch)):
                    errors_by_index[i + offset] = error_reason
                # Auth/model errors won't recover mid-run; fail remaining items.
                for j in range(i + batch_size, len(texts)):
                    errors_by_index[j] = error_reason
                errors.append(error_reason)
                break

            # Adaptive batch sizing: reduce on failure, down to single-item
            if batch_size > 1:
                batch_reductions += 1
                batch_size = max(1, batch_size // 2)
                continue

            # Single item failed; skip to avoid infinite loop
            errors_by_index[i] = error_reason or "failed_after_retries"
            errors.append(error_reason or "failed_after_retries")
            i += 1
            # Restore the full batch size for the remaining items; a single
            # bad item must not collapse the rest of the run to size 1.
            batch_size = initial_batch_size

        return EmbeddingsResult(
            embeddings_by_index=embeddings_by_index,
            errors_by_index=errors_by_index,
            retry_attempts=retry_attempts,
            batch_reductions=batch_reductions,
            rate_limit_errors=rate_limit_errors,
            error_summary="; ".join(errors) if errors else None,
            fatal_error=terminal_fatal_error,
            response_mismatch_retries=response_mismatch_retries,
            response_mismatch_batches=response_mismatch_batches,
        )


__all__ = [
    "Embedder",
    "OpenAIEmbedder",
    "EmbeddingsResult",
]

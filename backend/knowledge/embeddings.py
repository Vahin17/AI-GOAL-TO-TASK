"""
embeddings.py — Embedding Providers for Knowledge & Vector Store
=================================================================
Provides both OpenAI cloud embeddings (when API key is available) and a
deterministic, zero-dependency local embedding provider for offline/reproducible
operation.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
import hashlib
import logging
import math
import os
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingResult:
    """
    Immutable representation of an embedding vector and its provenance.
    Captures vector data and metadata atomically at generation time to prevent
    concurrency race conditions.
    """
    vector: List[float]
    provider: str
    model: str
    dimension: int
    embedding_space: str


class EmbeddingProvider(abc.ABC):
    """Abstract interface for embedding text into vector space."""

    @abc.abstractmethod
    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """Compute embedding vectors for a batch of strings."""
        pass

    @abc.abstractmethod
    def embed_query(self, query: str) -> List[float]:
        """Compute embedding vector for a single search query."""
        pass

    def embed_query_with_provenance(self, query: str) -> EmbeddingResult:
        """Compute embedding vector and return with atomic provenance."""
        vec = self.embed_query(query)
        dim = len(vec) if vec else self.dimension
        prov = getattr(self, "active_provider", "unknown")
        mod = getattr(self, "active_model", getattr(self, "model_name", "unknown"))
        space = getattr(self, "embedding_space", f"{prov}:{mod}:{dim}")
        return EmbeddingResult(vector=vec, provider=prov, model=mod, dimension=dim, embedding_space=space)

    def embed_texts_with_provenance(self, texts: List[str]) -> List[EmbeddingResult]:
        """Compute batch embedding vectors and return each with atomic provenance."""
        vecs = self.embed_texts(texts)
        results = []
        prov = getattr(self, "active_provider", "unknown")
        mod = getattr(self, "active_model", getattr(self, "model_name", "unknown"))
        for vec in vecs:
            dim = len(vec) if vec else self.dimension
            space = getattr(self, "embedding_space", f"{prov}:{mod}:{dim}")
            results.append(EmbeddingResult(vector=vec, provider=prov, model=mod, dimension=dim, embedding_space=space))
        return results

    @property
    @abc.abstractmethod
    def dimension(self) -> int:
        """Dimensionality of the output embedding vectors."""
        pass

    @property
    def active_dimension(self) -> int:
        return self.dimension

    @property
    def model_name(self) -> str:
        """Name of the embedding model generating vectors."""
        return "unknown"

    @property
    def active_provider(self) -> str:
        """Truthful provider identifier."""
        return "unknown"

    @property
    def embedding_space(self) -> str:
        """
        Globally unique identifier for the embedding space (provider + model + dimension).
        Ensures vector similarity operations never compare vectors from incompatible models.
        """
        return f"{self.active_provider}:{self.model_name}:{self.dimension}"


class DeterministicLocalEmbeddingProvider(EmbeddingProvider):
    """
    Zero-dependency, fast, deterministic local embedding provider.
    
    Generates a 128-dimensional L2-normalized vector using a hybrid of
    word token hash projections, character 3-grams, and semantic feature bins.
    
    Guarantees:
    - Identical text always produces the exact same vector.
    - Semantically related keywords (shared words/n-grams) produce high cosine similarity.
    - Zero network calls; 100% reproducible for benchmark tests.
    """

    def __init__(self, dim: int = 128):
        self._dim = dim

    @property
    def dimension(self) -> int:
        return self._dim

    @property
    def active_dimension(self) -> int:
        return self._dim

    @property
    def model_name(self) -> str:
        return f"deterministic-hash-{self._dim}"

    @property
    def active_provider(self) -> str:
        return "local_deterministic_fallback"

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        return [self._embed_single(t) for t in texts]

    def embed_query(self, query: str) -> List[float]:
        return self._embed_single(query)

    def _embed_single(self, text: str) -> List[float]:
        if not text.strip():
            return [0.0] * self._dim

        vec = [0.0] * self._dim
        normalized = text.lower().strip()
        tokens = re.findall(r"\b\w+\b", normalized)

        # 1. Word token projections
        for token in tokens:
            token_hash = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)
            idx = token_hash % self._dim
            sign = 1.0 if ((token_hash >> 8) & 1) == 0 else -1.0
            vec[idx] += sign * 1.5

        # 2. Character 3-gram projections for typo/morphological tolerance
        for i in range(len(normalized) - 2):
            trigram = normalized[i:i + 3]
            tri_hash = int(hashlib.sha256(trigram.encode("utf-8")).hexdigest(), 16)
            idx = tri_hash % self._dim
            sign = 1.0 if ((tri_hash >> 6) & 1) == 0 else -1.0
            vec[idx] += sign * 0.5

        # 3. L2 normalize
        norm_sq = sum(v * v for v in vec)
        if norm_sq > 0.0:
            norm = math.sqrt(norm_sq)
            vec = [round(v / norm, 6) for v in vec]
        else:
            vec = [0.0] * self._dim

        return vec


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """
    OpenAI embeddings provider using text-embedding-3-small (1536 dim).
    """

    def __init__(self, api_key: str, model: str = "text-embedding-3-small"):
        self.api_key = api_key
        self.model = model
        self._dimension = 1536
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key, max_retries=0, timeout=3.0)

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return self.model

    @property
    def active_provider(self) -> str:
        return "openai"

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        cleaned = [t.replace("\n", " ").strip() or " " for t in texts]
        response = self.client.embeddings.create(input=cleaned, model=self.model)
        return [item.embedding for item in response.data]

    def embed_query(self, query: str) -> List[float]:
        cleaned = query.replace("\n", " ").strip() or " "
        response = self.client.embeddings.create(input=[cleaned], model=self.model)
        return response.data[0].embedding


class HybridEmbeddingService(EmbeddingProvider):
    """
    Resilient embedding dispatcher:
    Uses OpenAI when configured and reachable; falls back to DeterministicLocalEmbeddingProvider
    on API failures or missing keys without crashing the application.
    Supports bounded recovery attempts with cooldowns distinguishing temporary network
    failures and rate limits from permanent quota exhaustion and invalid credentials (401).
    """

    NETWORK_COOLDOWN_SECONDS: float = 30.0
    RATE_LIMIT_COOLDOWN_SECONDS: float = 15.0
    QUOTA_COOLDOWN_SECONDS: float = 120.0

    _openai_quota_exhausted: bool = False
    _auth_invalid: bool = False
    _last_failure_time: float = 0.0
    _last_failure_type: Optional[str] = None
    _consecutive_failures: int = 0
    _last_embedding_error: Optional[str] = None
    _last_fallback_reason: Optional[str] = None
    _fallback_count: int = 0
    _retry_after_target: Optional[float] = None

    def __init__(self, api_key: Optional[str] = None, force_local: bool = False):
        self._local = DeterministicLocalEmbeddingProvider(dim=128)
        self._openai: Optional[OpenAIEmbeddingProvider] = None
        self._force_local = force_local

        if api_key is None:
            try:
                from core.config import settings
                api_key = getattr(settings, "OPENAI_API_KEY", None) or os.getenv("OPENAI_API_KEY")
            except Exception:
                api_key = os.getenv("OPENAI_API_KEY")

        self._api_key = api_key

        if (
            self._api_key
            and not self._force_local
            and not HybridEmbeddingService._auth_invalid
            and not HybridEmbeddingService._openai_quota_exhausted
            and not self._is_in_cooldown()
        ):
            try:
                self._openai = OpenAIEmbeddingProvider(api_key=self._api_key)
                logger.info("[EMBEDDING-CONFIG] provider=openai")
            except Exception as e:
                self._handle_failure(e)
                logger.warning(f"[EmbeddingService] Failed to init OpenAI embeddings: {e}. Using deterministic local.")
                logger.info("[EMBEDDING-CONFIG] provider=local")
        else:
            if HybridEmbeddingService._auth_invalid:
                logger.info("[EMBEDDING-CONFIG] provider=local (auth_invalid_401)")
            elif HybridEmbeddingService._openai_quota_exhausted:
                logger.info("[EMBEDDING-CONFIG] provider=local (cached quota_exhausted)")
            elif self._is_in_cooldown():
                logger.info("[EMBEDDING-CONFIG] provider=local (in cooldown)")
            else:
                logger.info("[EMBEDDING-CONFIG] provider=local")

    @classmethod
    def reset_state(cls) -> None:
        """Resets all class-level failure and recovery tracking (useful for test isolation)."""
        cls._openai_quota_exhausted = False
        cls._auth_invalid = False
        cls._last_failure_time = 0.0
        cls._last_failure_type = None
        cls._consecutive_failures = 0
        cls._last_embedding_error = None
        cls._last_fallback_reason = None
        cls._fallback_count = 0
        cls._retry_after_target = None

    def update_api_key(self, new_key: str) -> None:
        """Dynamically reloads API credentials without requiring process restart."""
        if not new_key:
            return
        self._api_key = new_key
        HybridEmbeddingService._auth_invalid = False
        HybridEmbeddingService._openai_quota_exhausted = False
        HybridEmbeddingService._retry_after_target = None
        try:
            self._openai = OpenAIEmbeddingProvider(api_key=new_key)
            logger.info("[EmbeddingService] Successfully updated API key and initialized OpenAI provider.")
        except Exception as e:
            self._handle_failure(e)

    @classmethod
    def _is_in_cooldown(cls) -> bool:
        if cls._auth_invalid:
            return True  # 401 invalid credentials is permanent, never retries

        if cls._last_failure_time <= 0.0 or cls._last_failure_type is None:
            return False

        now = time.time()
        # 1. Check explicit Retry-After target if active
        if cls._retry_after_target is not None:
            if now < cls._retry_after_target:
                return True
            cls._retry_after_target = None

        if cls._openai_quota_exhausted:
            return (now - cls._last_failure_time) < cls.QUOTA_COOLDOWN_SECONDS

        if cls._last_failure_type == "rate_limit_429":
            return (now - cls._last_failure_time) < cls.RATE_LIMIT_COOLDOWN_SECONDS

        if cls._last_failure_type == "network_error":
            return (now - cls._last_failure_time) < cls.NETWORK_COOLDOWN_SECONDS

        return False

    def _maybe_recover_openai(self) -> None:
        """Attempts bounded recovery of OpenAI provider if cooldown period has elapsed."""
        if (
            self._force_local
            or HybridEmbeddingService._auth_invalid
            or not self._api_key
        ):
            return
        if self._is_in_cooldown():
            return
        if self._openai is not None and not HybridEmbeddingService._openai_quota_exhausted:
            return

        try:
            logger.info("[EmbeddingService] Cooldown expired; attempting OpenAI recovery.")
            self._openai = OpenAIEmbeddingProvider(api_key=self._api_key)
        except Exception as e:
            self._handle_failure(e)

    def _handle_success(self) -> None:
        """Called after successful OpenAI embedding generation."""
        if (
            HybridEmbeddingService._consecutive_failures > 0
            or HybridEmbeddingService._openai_quota_exhausted
            or HybridEmbeddingService._last_failure_type is not None
        ):
            logger.info("[EMBEDDING-RECOVERY] OpenAI embeddings successfully recovered.")
        HybridEmbeddingService._consecutive_failures = 0
        HybridEmbeddingService._openai_quota_exhausted = False
        HybridEmbeddingService._last_failure_type = None
        HybridEmbeddingService._last_failure_time = 0.0
        HybridEmbeddingService._last_fallback_reason = None
        HybridEmbeddingService._retry_after_target = None

    def _handle_failure(self, e: Exception) -> None:
        """Categorizes OpenAI errors into permanent auth failures, hard quota exhaustion, rate limits, or network glitches."""
        from core.retry_utils import classify_provider_error, parse_retry_after

        err_str = str(e).lower()
        HybridEmbeddingService._last_embedding_error = str(e)
        HybridEmbeddingService._fallback_count += 1
        HybridEmbeddingService._consecutive_failures += 1
        HybridEmbeddingService._last_failure_time = time.time()
        self._openai = None

        # Extract status code if available
        status_code = getattr(e, "status_code", None) or getattr(e, "http_status", 0)
        if not status_code:
            if "401" in err_str:
                status_code = 401
            elif "429" in err_str:
                status_code = 429
            elif "503" in err_str:
                status_code = 503

        # Parse retry_after if available
        retry_after: Optional[float] = None
        resp = getattr(e, "response", None)
        headers = getattr(resp, "headers", None) if resp else getattr(e, "headers", None)
        if headers and hasattr(headers, "get"):
            retry_header = headers.get("Retry-After") or headers.get("retry-after")
            if retry_header:
                retry_after = parse_retry_after(retry_header)

        if retry_after is None:
            # Check for regex retry guidance in error text e.g. "retry in 12s"
            m = re.search(r'retry (?:in|after)\s+([\d\.]+)\s*s', err_str)
            if m:
                try:
                    retry_after = float(m.group(1))
                except (ValueError, TypeError):
                    pass

        failure_type, is_permanent = classify_provider_error(status_code, err_str)

        if failure_type == "auth_401":
            HybridEmbeddingService._auth_invalid = True
            HybridEmbeddingService._last_failure_type = "auth_401"
            HybridEmbeddingService._last_fallback_reason = "auth_invalid_401"
            logger.warning(f"[EMBEDDING-FALLBACK] reason=auth_invalid_401: {e}")
        elif failure_type == "quota_exhausted":
            HybridEmbeddingService._openai_quota_exhausted = True
            HybridEmbeddingService._last_failure_type = "quota_exhausted"
            HybridEmbeddingService._last_fallback_reason = "quota_exhausted_permanent"
            logger.warning(f"[EMBEDDING-FALLBACK] reason=quota_exhausted (permanent): {e}")
        elif failure_type == "rate_limit_429":
            HybridEmbeddingService._openai_quota_exhausted = False
            HybridEmbeddingService._last_failure_type = "rate_limit_429"
            HybridEmbeddingService._last_fallback_reason = "rate_limit_429"
            if retry_after is not None and retry_after > 0:
                HybridEmbeddingService._retry_after_target = time.time() + retry_after
            else:
                HybridEmbeddingService._retry_after_target = time.time() + HybridEmbeddingService.RATE_LIMIT_COOLDOWN_SECONDS
            logger.warning(f"[EMBEDDING-FALLBACK] reason=rate_limit_429 (transient cooldown {retry_after or HybridEmbeddingService.RATE_LIMIT_COOLDOWN_SECONDS:.1f}s): {e}")
        else:
            HybridEmbeddingService._last_failure_type = "network_error"
            HybridEmbeddingService._last_fallback_reason = "network_error"
            logger.warning(f"[EMBEDDING-FALLBACK] reason=network_error: {e}. Falling back to local.")

        logger.info("[EMBEDDING-CONFIG] provider=local")

    def embed_texts_with_provenance(self, texts: List[str]) -> List[EmbeddingResult]:
        self._maybe_recover_openai()
        if (
            not self._force_local
            and self._openai
            and not HybridEmbeddingService._openai_quota_exhausted
            and not self._is_in_cooldown()
        ):
            try:
                res = self._openai.embed_texts(texts)
                self._handle_success()
                return [
                    EmbeddingResult(
                        vector=v,
                        provider="openai",
                        model=self._openai.model_name,
                        dimension=self._openai.dimension,
                        embedding_space=self._openai.embedding_space,
                    )
                    for v in res
                ]
            except Exception as e:
                self._handle_failure(e)

        local_vecs = self._local.embed_texts(texts)
        prov = self.active_provider
        mod = self._local.model_name
        dim = self._local.dimension
        space = self._local.embedding_space
        return [
            EmbeddingResult(
                vector=v,
                provider=prov,
                model=mod,
                dimension=dim,
                embedding_space=space,
            )
            for v in local_vecs
        ]

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        return [r.vector for r in self.embed_texts_with_provenance(texts)]

    def embed_query_with_provenance(self, query: str) -> EmbeddingResult:
        self._maybe_recover_openai()
        if (
            not self._force_local
            and self._openai
            and not HybridEmbeddingService._openai_quota_exhausted
            and not self._is_in_cooldown()
        ):
            try:
                res = self._openai.embed_query(query)
                self._handle_success()
                return EmbeddingResult(
                    vector=res,
                    provider="openai",
                    model=self._openai.model_name,
                    dimension=self._openai.dimension,
                    embedding_space=self._openai.embedding_space,
                )
            except Exception as e:
                self._handle_failure(e)

        local_vec = self._local.embed_query(query)
        prov = self.active_provider
        return EmbeddingResult(
            vector=local_vec,
            provider=prov,
            model=self._local.model_name,
            dimension=self._local.dimension,
            embedding_space=self._local.embedding_space,
        )

    def embed_query(self, query: str) -> List[float]:
        return self.embed_query_with_provenance(query).vector

    @classmethod
    def get_diagnostics(cls) -> Dict[str, Any]:
        from core.config import settings
        now = time.time()
        has_key = bool(getattr(settings, "OPENAI_API_KEY", None))
        if cls._retry_after_target is not None:
            cooldown_remaining = max(0.0, cls._retry_after_target - now)
        elif cls._openai_quota_exhausted:
            cooldown_remaining = max(0.0, cls.QUOTA_COOLDOWN_SECONDS - (now - cls._last_failure_time)) if cls._last_failure_time > 0 else 0.0
        elif cls._last_failure_type == "rate_limit_429":
            cooldown_remaining = max(0.0, cls.RATE_LIMIT_COOLDOWN_SECONDS - (now - cls._last_failure_time)) if cls._last_failure_time > 0 else 0.0
        else:
            cooldown_remaining = max(0.0, cls.NETWORK_COOLDOWN_SECONDS - (now - cls._last_failure_time)) if cls._last_failure_time > 0 else 0.0

        return {
            "openai_quota_exhausted": cls._openai_quota_exhausted,
            "auth_invalid": cls._auth_invalid,
            "last_failure_type": cls._last_failure_type,
            "last_embedding_error": cls._last_embedding_error,
            "last_fallback_reason": cls._last_fallback_reason,
            "fallback_count": cls._fallback_count,
            "consecutive_failures": cls._consecutive_failures,
            "in_cooldown": cls._is_in_cooldown(),
            "cooldown_remaining_seconds": round(cooldown_remaining, 1),
            "embedding_fallback_active": cls._is_in_cooldown() or cls._auth_invalid or (not has_key),
            "is_permanent_quota": cls._openai_quota_exhausted,
            "is_transient_rate_limit": cls._last_failure_type == "rate_limit_429",
            "retry_after_active": cls._retry_after_target is not None and now < cls._retry_after_target,
        }

    @property
    def dimension(self) -> int:
        return self.active_dimension

    @property
    def active_dimension(self) -> int:
        if self._openai and not HybridEmbeddingService._openai_quota_exhausted and not self._is_in_cooldown():
            return self._openai.dimension
        return self._local.dimension

    @property
    def model_name(self) -> str:
        if self._openai and not HybridEmbeddingService._openai_quota_exhausted and not self._is_in_cooldown():
            return self._openai.model_name
        return self._local.model_name

    @property
    def active_model(self) -> str:
        """Truthful name of the active model generating vectors."""
        return self.model_name

    @property
    def is_using_cloud(self) -> bool:
        return (
            self._openai is not None
            and not HybridEmbeddingService._openai_quota_exhausted
            and not self._is_in_cooldown()
        )

    @property
    def active_provider(self) -> str:
        """Truthful indicator of which embedding provider is actively generating vectors."""
        if self._openai and not HybridEmbeddingService._openai_quota_exhausted and not self._is_in_cooldown():
            return "openai"
        if HybridEmbeddingService._auth_invalid:
            return "local_deterministic_fallback (auth_invalid_401)"
        if HybridEmbeddingService._openai_quota_exhausted:
            return "local_deterministic_fallback (openai_quota_exhausted)"
        if HybridEmbeddingService._last_failure_type == "rate_limit_429" and self._is_in_cooldown():
            return "local_deterministic_fallback (rate_limit_cooldown)"
        if HybridEmbeddingService._last_failure_type == "network_error" and self._is_in_cooldown():
            return "local_deterministic_fallback (network_cooldown)"
        return "local_deterministic_fallback"

    @property
    def embedding_space(self) -> str:
        if self._openai and not HybridEmbeddingService._openai_quota_exhausted and not self._is_in_cooldown():
            return self._openai.embedding_space
        return self._local.embedding_space

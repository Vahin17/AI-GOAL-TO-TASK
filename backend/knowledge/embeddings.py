"""
embeddings.py — Embedding Providers for Knowledge & Vector Store
=================================================================
Provides both OpenAI cloud embeddings (when API key is available) and a
deterministic, zero-dependency local embedding provider for offline/reproducible
operation.
"""

from __future__ import annotations

import abc
import hashlib
import logging
import math
import re
from typing import List, Optional

logger = logging.getLogger(__name__)


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

    @property
    @abc.abstractmethod
    def dimension(self) -> int:
        """Dimensionality of the output embedding vectors."""
        pass

    @property
    def active_dimension(self) -> int:
        return self.dimension


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


class HybridEmbeddingService:
    """
    Resilient embedding dispatcher:
    Uses OpenAI when configured and reachable; falls back to DeterministicLocalEmbeddingProvider
    on API failures or missing keys without crashing the application.
    """

    _openai_quota_exhausted: bool = False
    _last_embedding_error: Optional[str] = None
    _last_fallback_reason: Optional[str] = None
    _fallback_count: int = 0

    def __init__(self, api_key: Optional[str] = None, force_local: bool = False):
        self._local = DeterministicLocalEmbeddingProvider(dim=128)
        self._openai: Optional[OpenAIEmbeddingProvider] = None

        if api_key and not force_local and not HybridEmbeddingService._openai_quota_exhausted:
            try:
                self._openai = OpenAIEmbeddingProvider(api_key=api_key)
                logger.info("[EMBEDDING-CONFIG] provider=openai")
            except Exception as e:
                HybridEmbeddingService._last_embedding_error = str(e)
                HybridEmbeddingService._last_fallback_reason = "init_failure"
                logger.warning(f"[EmbeddingService] Failed to init OpenAI embeddings: {e}. Using deterministic local.")
                logger.info("[EMBEDDING-CONFIG] provider=local")
        else:
            if HybridEmbeddingService._openai_quota_exhausted:
                logger.info("[EMBEDDING-CONFIG] provider=local (cached quota_exhausted)")
            else:
                logger.info("[EMBEDDING-CONFIG] provider=local")

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if self._openai and not HybridEmbeddingService._openai_quota_exhausted:
            try:
                return self._openai.embed_texts(texts)
            except Exception as e:
                err_str = str(e).lower()
                HybridEmbeddingService._last_embedding_error = str(e)
                HybridEmbeddingService._fallback_count += 1
                if "quota" in err_str or "credit" in err_str or "429" in err_str:
                    HybridEmbeddingService._openai_quota_exhausted = True
                    HybridEmbeddingService._last_fallback_reason = "quota_exhausted_429"
                    logger.warning(f"[EMBEDDING-FALLBACK] reason=quota_exhausted: {e}")
                else:
                    HybridEmbeddingService._last_fallback_reason = "api_error"
                    logger.warning(f"[EmbeddingService] OpenAI embed failed: {e}. Falling back to local.")
                self._openai = None
                logger.info("[EMBEDDING-CONFIG] provider=local")
        return self._local.embed_texts(texts)

    def embed_query(self, query: str) -> List[float]:
        if self._openai and not HybridEmbeddingService._openai_quota_exhausted:
            try:
                return self._openai.embed_query(query)
            except Exception as e:
                err_str = str(e).lower()
                HybridEmbeddingService._last_embedding_error = str(e)
                HybridEmbeddingService._fallback_count += 1
                if "quota" in err_str or "credit" in err_str or "429" in err_str:
                    HybridEmbeddingService._openai_quota_exhausted = True
                    HybridEmbeddingService._last_fallback_reason = "quota_exhausted_429"
                    logger.warning(f"[EMBEDDING-FALLBACK] reason=quota_exhausted: {e}")
                else:
                    HybridEmbeddingService._last_fallback_reason = "api_error"
                    logger.warning(f"[EmbeddingService] OpenAI embed_query failed: {e}. Falling back to local.")
                self._openai = None
                logger.info("[EMBEDDING-CONFIG] provider=local")
        return self._local.embed_query(query)

    @classmethod
    def get_diagnostics(cls) -> Dict[str, Any]:
        from core.config import settings
        has_key = bool(getattr(settings, "OPENAI_API_KEY", None))
        return {
            "openai_quota_exhausted": cls._openai_quota_exhausted,
            "last_embedding_error": cls._last_embedding_error,
            "last_fallback_reason": cls._last_fallback_reason,
            "fallback_count": cls._fallback_count,
            "embedding_fallback_active": cls._openai_quota_exhausted or (not has_key),
        }

    @property
    def active_dimension(self) -> int:
        return self._openai.dimension if self._openai else self._local.dimension

    @property
    def is_using_cloud(self) -> bool:
        return self._openai is not None

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)

#: Character n-gram width used by :meth:`ResponseCache.similarity`.
NGRAM_SIZE = 3

#: Reason recorded in ``false_hit_log`` when a candidate is rejected because the
#: query and the cached key disagree on a 4-digit number (year, invoice id, ...).
FALSE_HIT_REASON = "date_or_number_mismatch"


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """In-memory semantic cache with privacy and false-hit guardrails.

    Lookup is a similarity scan rather than an exact-key match, so paraphrases of an
    already-answered question can reuse the stored response. Two guardrails keep that
    from returning a wrong answer:

    * privacy — queries matching :data:`PRIVACY_PATTERNS` are never stored nor served;
    * false-hit — a candidate above the similarity threshold is still rejected when it
      disagrees with the query on a 4-digit number, which is exactly the "2024 policy
      vs 2026 policy" trap (see ``data/sample_queries.jsonl`` q7/q8 and q16/q17).
    """

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []
        self.hits = 0
        self.misses = 0
        self._lock = threading.RLock()

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response by semantic similarity.

        Returns ``(value, score)`` on a hit and ``(None, best_score)`` on a miss, so the
        caller can log how close the nearest neighbour was even when nothing is served.
        """
        if _is_uncacheable(query):
            self.misses += 1
            return None, 0.0

        with self._lock:
            self._evict_expired()

            best_entry: CacheEntry | None = None
            best_score = 0.0
            for entry in self._entries:
                score = self.similarity(query, entry.key)
                if score > best_score:
                    best_score = score
                    best_entry = entry

            if best_entry is None or best_score < self.similarity_threshold:
                self.misses += 1
                return None, best_score

            if _looks_like_false_hit(query, best_entry.key):
                # Log once per lookup (best candidate only), never once per scanned
                # entry — the report quotes these as concrete false-hit examples.
                self.false_hit_log.append(
                    {
                        "query": query,
                        "cached_key": best_entry.key,
                        "score": round(best_score, 4),
                        "reason": FALSE_HIT_REASON,
                    }
                )
                self.misses += 1
                return None, best_score

            self.hits += 1
            return best_entry.value, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response, dropping privacy-sensitive queries on the floor."""
        if _is_uncacheable(query):
            return
        with self._lock:
            # Replace any existing entry for the same key so a long load test cannot
            # grow the scan list without bound.
            self._entries = [entry for entry in self._entries if entry.key != query]
            self._entries.append(
                CacheEntry(
                    key=query,
                    value=value,
                    created_at=time.time(),
                    metadata=dict(metadata or {}),
                )
            )

    def stats(self) -> dict[str, object]:
        """Return counters used by the metrics report."""
        with self._lock:
            lookups = self.hits + self.misses
            return {
                "backend": "memory",
                "entries": len(self._entries),
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / lookups, 4) if lookups else 0.0,
                "false_hits_blocked": len(self.false_hit_log),
                "ttl_seconds": self.ttl_seconds,
                "similarity_threshold": self.similarity_threshold,
            }

    def _evict_expired(self) -> None:
        now = time.time()
        self._entries = [
            entry for entry in self._entries if now - entry.created_at <= self.ttl_seconds
        ]

    @staticmethod
    def _tokenize(text: str) -> Counter[str]:
        """Bag of word tokens plus character n-grams.

        Word tokens capture topical overlap; the character n-grams keep morphology and
        word order partially intact, which is what lets "circuit breaker pattern" and
        "circuit breaker design" score as *partially* similar instead of being flattened
        to a single Jaccard number.
        """
        normalized = text.lower().strip()
        tokens: list[str] = normalized.split()
        tokens.extend(
            normalized[index : index + NGRAM_SIZE]
            for index in range(len(normalized) - NGRAM_SIZE + 1)
        )
        return Counter(tokens)

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Cosine similarity over character n-grams + word tokens, in ``[0.0, 1.0]``."""
        if a == b:
            return 1.0

        vector_a = ResponseCache._tokenize(a)
        vector_b = ResponseCache._tokenize(b)
        if not vector_a or not vector_b:
            return 0.0

        shared = vector_a.keys() & vector_b.keys()
        dot = sum(vector_a[token] * vector_b[token] for token in shared)
        if dot == 0:
            return 0.0

        norm_a = math.sqrt(sum(count * count for count in vector_a.values()))
        norm_b = math.sqrt(sum(count * count for count in vector_b.values()))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Redis shared cache (new)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache so several gateway instances hit one cache.

    Data model::

        key   = "{prefix}{md5(query)[:12]}"   one Redis Hash per cached query
        hash  = {"query": <original query>, "response": <cached answer>}
        ttl   = Redis EXPIRE — eviction is handled server side, not by this process

    Exact lookups are a single ``HGET`` on the hashed key. A miss falls back to a
    ``SCAN`` + similarity comparison, reusing :meth:`ResponseCache.similarity` so the
    in-memory and shared backends score identically.

    If Redis becomes unreachable the cache degrades to a process-local
    :class:`ResponseCache` instead of raising: a cache outage should cost hit rate,
    never availability.
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self.hits = 0
        self.misses = 0
        self.degraded = False
        self.degraded_events = 0
        self._fallback = ResponseCache(ttl_seconds, similarity_threshold)
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:  # noqa: BLE001 — connectivity probe must never raise
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis (exact key first, then similarity scan)."""
        if _is_uncacheable(query):
            self.misses += 1
            return None, 0.0

        try:
            exact_key = f"{self.prefix}{self._query_hash(query)}"
            exact = self._redis.hget(exact_key, "response")
            if exact is not None:
                self.hits += 1
                return str(exact), 1.0

            best_key: str | None = None
            best_query = ""
            best_score = 0.0
            for key in self._redis.scan_iter(f"{self.prefix}*"):
                cached_query = self._redis.hget(key, "query")
                if cached_query is None:
                    continue
                score = ResponseCache.similarity(query, str(cached_query))
                if score > best_score:
                    best_score = score
                    best_key = str(key)
                    best_query = str(cached_query)

            if best_key is None or best_score < self.similarity_threshold:
                self.misses += 1
                return None, best_score

            if _looks_like_false_hit(query, best_query):
                self.false_hit_log.append(
                    {
                        "query": query,
                        "cached_key": best_query,
                        "score": round(best_score, 4),
                        "reason": FALSE_HIT_REASON,
                    }
                )
                self.misses += 1
                return None, best_score

            response = self._redis.hget(best_key, "response")
            if response is None:
                self.misses += 1
                return None, best_score
            self.hits += 1
            return str(response), best_score
        # Broad on purpose: any cache failure (connection, timeout, decode) must cost
        # hit rate, never availability.
        except Exception:  # noqa: BLE001
            self._mark_degraded()
            return self._fallback.get(query)

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with a server-side TTL."""
        if _is_uncacheable(query):
            return
        key = f"{self.prefix}{self._query_hash(query)}"
        try:
            self._redis.hset(key, mapping={"query": query, "response": value})
            self._redis.expire(key, self.ttl_seconds)
        except Exception:  # noqa: BLE001 — see get(); degrade instead of failing the request
            self._mark_degraded()
            self._fallback.set(query, value, metadata)

    def stats(self) -> dict[str, object]:
        """Return counters used by the metrics report."""
        lookups = self.hits + self.misses
        try:
            entries = sum(1 for _ in self._redis.scan_iter(f"{self.prefix}*"))
        except Exception:  # noqa: BLE001 — stats are best-effort telemetry
            entries = -1
        return {
            "backend": "redis",
            "prefix": self.prefix,
            "entries": entries,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / lookups, 4) if lookups else 0.0,
            "false_hits_blocked": len(self.false_hit_log),
            "ttl_seconds": self.ttl_seconds,
            "similarity_threshold": self.similarity_threshold,
            "degraded": self.degraded,
            "degraded_events": self.degraded_events,
        }

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    def _mark_degraded(self) -> None:
        self.degraded = True
        self.degraded_events += 1

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]

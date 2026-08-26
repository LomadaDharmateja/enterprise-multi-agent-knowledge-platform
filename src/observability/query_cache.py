"""Query-level result cache (M5 Task 3).

Keyed on a hash of the normalised question. A hit returns the stored final response
without touching a database or an LLM, which is the whole point: the M5 cost table puts
an answered query at $0.00456 and ~15 s, so a hit is worth all of it.

Two backends behind one interface. In-memory LRU is the default and needs no
infrastructure; Redis is selectable with `CACHE_BACKEND=redis` for a deployment with
more than one process, where an in-process cache would be per-worker and mostly cold.

**What is deliberately NOT cached:** anything keyed on something other than the question
text. The plan, the evidence and the answer are all downstream of the question, and the
corpus is static between ingests, so the question alone is a sound key. If the corpus
becomes mutable, this key stops being sound and the cache needs a corpus version in it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import OrderedDict
from typing import Any

DEFAULT_MAX_SIZE = 100
CACHE_VERSION = "v1"


def normalise_question(question: str) -> str:
    """Collapse whitespace and case so trivial variants share an entry.

    Deliberately conservative: no stemming, no stopword removal, no synonym folding.
    Two questions that differ in any word are different questions, because the
    evidence they should retrieve differs.
    """
    return re.sub(r"\s+", " ", (question or "").strip().lower())


def cache_key(question: str) -> str:
    digest = hashlib.sha256(normalise_question(question).encode("utf-8")).hexdigest()
    return f"{CACHE_VERSION}:{digest[:32]}"


class CacheStats:
    def __init__(self) -> None:
        self.hits = 0
        self.misses = 0
        self.stores = 0
        self.evictions = 0
        self._lock = threading.Lock()

    def record_hit(self) -> None:
        with self._lock:
            self.hits += 1

    def record_miss(self) -> None:
        with self._lock:
            self.misses += 1

    def record_store(self, evicted: bool = False) -> None:
        with self._lock:
            self.stores += 1
            if evicted:
                self.evictions += 1

    def reset(self) -> None:
        with self._lock:
            self.hits = self.misses = self.stores = self.evictions = 0

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.lookups if self.lookups else 0.0

    def as_dict(self, mean_tokens_per_query: dict[str, float] | None = None) -> dict:
        tokens = mean_tokens_per_query or {}
        saved_in = self.hits * tokens.get("input", 0.0)
        saved_out = self.hits * tokens.get("output", 0.0)

        return {
            "hits": self.hits,
            "misses": self.misses,
            "lookups": self.lookups,
            "hit_rate": round(self.hit_rate, 4),
            "stores": self.stores,
            "evictions": self.evictions,
            "estimated_tokens_saved": {
                "input": round(saved_in),
                "output": round(saved_out),
                "total": round(saved_in + saved_out),
            },
            "estimated_cost_saved_usd": round(
                saved_in * 0.25 / 1e6 + saved_out * 1.50 / 1e6, 6
            ),
        }


class InMemoryLRUCache:
    """Bounded LRU. Thread-safe; the concurrency test drives it from 5 threads."""

    backend = "memory"

    def __init__(self, max_size: int = DEFAULT_MAX_SIZE) -> None:
        self.max_size = max_size
        self._entries: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            if key not in self._entries:
                return None

            self._entries.move_to_end(key)
            return self._entries[key]

    def set(self, key: str, value: Any) -> bool:
        with self._lock:
            evicted = False

            if key in self._entries:
                self._entries.move_to_end(key)
            elif len(self._entries) >= self.max_size:
                self._entries.popitem(last=False)
                evicted = True

            self._entries[key] = value
            return evicted

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


class RedisCache:
    """Optional shared backend for multi-process deployments.

    Not exercised by the test suite -- there is no Redis in this environment -- so it
    is written to fail loudly at construction rather than silently degrade to a cache
    that never hits.
    """

    backend = "redis"

    def __init__(self, url: str, ttl_seconds: int = 3600, prefix: str = "eaiq") -> None:
        try:
            import redis  # noqa: F401
        except ImportError as exc:  # pragma: no cover - no redis in this environment
            raise RuntimeError(
                "CACHE_BACKEND=redis but the `redis` package is not installed."
            ) from exc

        import redis

        self.ttl_seconds = ttl_seconds
        self.prefix = prefix
        self._client = redis.Redis.from_url(url, decode_responses=True)
        self._client.ping()

    def _k(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    def get(self, key: str) -> Any | None:  # pragma: no cover
        raw = self._client.get(self._k(key))
        return json.loads(raw) if raw else None

    def set(self, key: str, value: Any) -> bool:  # pragma: no cover
        self._client.setex(
            self._k(key), self.ttl_seconds, json.dumps(value, default=str)
        )
        return False

    def clear(self) -> None:  # pragma: no cover
        for k in self._client.scan_iter(f"{self.prefix}:*"):
            self._client.delete(k)

    def __len__(self) -> int:  # pragma: no cover
        return sum(1 for _ in self._client.scan_iter(f"{self.prefix}:*"))


class QueryCache:
    def __init__(self, backend: Any, stats: CacheStats | None = None) -> None:
        self.backend = backend
        self.stats = stats or CacheStats()

    def get(self, question: str) -> Any | None:
        value = self.backend.get(cache_key(question))

        if value is None:
            self.stats.record_miss()
            return None

        self.stats.record_hit()
        return value

    def set(self, question: str, value: Any) -> None:
        evicted = self.backend.set(cache_key(question), value)
        self.stats.record_store(evicted=evicted)

    def clear(self) -> None:
        self.backend.clear()
        self.stats.reset()

    def as_dict(self, mean_tokens_per_query: dict[str, float] | None = None) -> dict:
        payload = self.stats.as_dict(mean_tokens_per_query)
        payload["backend"] = self.backend.backend
        payload["entries"] = len(self.backend)
        payload["max_size"] = getattr(self.backend, "max_size", None)
        payload["enabled"] = cache_enabled()
        return payload


def cache_enabled() -> bool:
    return os.getenv("QUERY_CACHE_ENABLED", "true").strip().lower() not in {
        "false", "0", "no",
    }


def build_cache() -> QueryCache:
    backend_name = os.getenv("CACHE_BACKEND", "memory").strip().lower()

    if backend_name == "redis":
        return QueryCache(
            RedisCache(
                url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                ttl_seconds=int(os.getenv("CACHE_TTL_SECONDS", "3600")),
            )
        )

    return QueryCache(
        InMemoryLRUCache(max_size=int(os.getenv("CACHE_MAX_SIZE", str(DEFAULT_MAX_SIZE))))
    )


_CACHE: QueryCache | None = None
_CACHE_LOCK = threading.Lock()


def get_cache() -> QueryCache:
    global _CACHE

    with _CACHE_LOCK:
        if _CACHE is None:
            _CACHE = build_cache()

        return _CACHE


def reset_cache() -> None:
    global _CACHE

    with _CACHE_LOCK:
        _CACHE = None

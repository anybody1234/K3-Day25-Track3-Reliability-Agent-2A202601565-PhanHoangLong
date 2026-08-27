from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class ProviderConfig(BaseModel):
    name: str
    fail_rate: float = Field(ge=0.0, le=1.0)
    base_latency_ms: int = Field(gt=0)
    cost_per_1k_tokens: float = Field(ge=0.0)


class CircuitBreakerConfig(BaseModel):
    failure_threshold: int = Field(gt=0)
    reset_timeout_seconds: float = Field(gt=0)
    success_threshold: int = Field(gt=0)


class CacheConfig(BaseModel):
    enabled: bool = True
    backend: str = "memory"  # "memory" or "redis"
    ttl_seconds: int = Field(gt=0)
    similarity_threshold: float = Field(ge=0.0, le=1.0)
    redis_url: str = "redis://localhost:6379/0"


class LoadTestConfig(BaseModel):
    requests: int = Field(gt=0)
    #: Number of worker threads. 1 keeps the run sequential and reproducible;
    #: values > 1 exercise the shared circuit breaker under concurrent load.
    concurrency: int = Field(default=1, gt=0)
    #: Seed for query selection and for the provider failure/latency RNG, so a
    #: sequential run is byte-for-byte reproducible.
    seed: int = 1234


class ScenarioConfig(BaseModel):
    name: str
    description: str = ""
    provider_overrides: dict[str, float] = Field(default_factory=dict)
    #: Per-scenario override of ``cache.enabled``; None inherits the global setting.
    #: Used by the cache vs no-cache comparison scenario.
    cache_enabled: bool | None = None
    #: Per-scenario override of ``load_test.concurrency``; None inherits the global value.
    concurrency: int | None = Field(default=None, gt=0)
    #: Optional spend cap handed to the gateway for cost-aware routing.
    cost_budget: float | None = Field(default=None, gt=0)
    #: Bounded retry on the last provider in the chain. 0 (default) reproduces the
    #: no-retry behaviour specified for the lab.
    max_retries_per_request: int = Field(default=0, ge=0)
    #: Share of total traffic that may be spent on retries before they are refused.
    retry_budget_ratio: float = Field(default=0.0, ge=0.0, le=1.0)


class LabConfig(BaseModel):
    providers: list[ProviderConfig]
    circuit_breaker: CircuitBreakerConfig
    cache: CacheConfig
    load_test: LoadTestConfig
    scenarios: list[ScenarioConfig] = Field(default_factory=list)


def load_config(path: str | Path) -> LabConfig:
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text())
    return LabConfig.model_validate(raw)

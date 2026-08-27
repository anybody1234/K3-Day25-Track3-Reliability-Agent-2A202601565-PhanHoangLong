"""Extra reliability tests beyond the handout suite.

These cover the properties the rubric grades as *evidence* rather than as unit
behaviour: no retry storm, no reset-timer restart while OPEN, thread safety under
concurrent load, privacy coverage over the real query corpus, and the SLO floor.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from reliability_lab.cache import ResponseCache, _is_uncacheable
from reliability_lab.chaos import (
    build_gateway,
    calculate_recovery_time_ms,
    load_queries,
    run_scenario,
)
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitState
from reliability_lab.config import LabConfig, ScenarioConfig, load_config
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.providers import FakeLLMProvider, ProviderError

REPO_ROOT = Path(__file__).resolve().parents[1]


class CountingProvider(FakeLLMProvider):
    """Provider that records how many times it was actually invoked."""

    def __init__(self, name: str, fail_rate: float) -> None:
        super().__init__(name, fail_rate, base_latency_ms=1, cost_per_1k_tokens=0.001)
        self.calls = 0

    def complete(self, prompt: str):  # type: ignore[no-untyped-def]
        self.calls += 1
        return super().complete(prompt)


# --------------------------------------------------------------------------- #
# Circuit breaker: no retry storm
# --------------------------------------------------------------------------- #


def test_open_circuit_stops_calling_the_provider() -> None:
    """Once OPEN, the breaker must fail fast without touching the provider."""
    provider = CountingProvider("dead", fail_rate=1.0)
    breaker = CircuitBreaker("dead", failure_threshold=3, reset_timeout_seconds=60)
    gateway = ReliabilityGateway([provider], {"dead": breaker})

    for _ in range(50):
        gateway.complete("anything")

    assert breaker.state is CircuitState.OPEN
    # 3 calls to trip the breaker, then nothing: 47 requests were shed, not retried.
    assert provider.calls == 3


def test_failures_while_open_do_not_restart_the_reset_timer() -> None:
    """Recording a failure while already OPEN must not push opened_at forward.

    If it did, a steady stream of failures would keep resetting the timer and the
    circuit could never reach HALF_OPEN, so it would never recover.
    """
    breaker = CircuitBreaker("p", failure_threshold=1, reset_timeout_seconds=0.2)
    breaker.record_failure()
    first_opened_at = breaker.opened_at
    assert first_opened_at is not None

    for _ in range(5):
        breaker.record_failure()

    assert breaker.opened_at == first_opened_at
    open_edges = [t for t in breaker.transition_log if t["to"] == "open"]
    assert len(open_edges) == 1

    time.sleep(0.25)
    assert breaker.allow_request()
    assert breaker.state is CircuitState.HALF_OPEN


def test_recovery_time_is_measured_between_open_and_closed() -> None:
    provider = FakeLLMProvider("p", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.001)
    breaker = CircuitBreaker("p", failure_threshold=1, reset_timeout_seconds=0.1)
    gateway = ReliabilityGateway([provider], {"p": breaker})

    assert calculate_recovery_time_ms(gateway) is None  # nothing opened yet

    breaker.record_failure()
    time.sleep(0.15)
    gateway.complete("recover please")  # probe succeeds -> CLOSED

    recovery_ms = calculate_recovery_time_ms(gateway)
    assert recovery_ms is not None
    assert 100.0 <= recovery_ms < 2000.0


# --------------------------------------------------------------------------- #
# Gateway routing
# --------------------------------------------------------------------------- #


def test_route_detail_names_the_serving_provider() -> None:
    primary = FakeLLMProvider("primary", 1.0, 1, 0.01)
    backup = FakeLLMProvider("backup", 0.0, 1, 0.006)
    breakers = {
        "primary": CircuitBreaker("primary", failure_threshold=1, reset_timeout_seconds=10),
        "backup": CircuitBreaker("backup", failure_threshold=3, reset_timeout_seconds=10),
    }
    result = ReliabilityGateway([primary, backup], breakers).complete("q")

    assert result.route == "fallback"
    assert result.route_detail == "fallback:backup"


def test_static_fallback_error_names_the_failing_providers() -> None:
    primary = FakeLLMProvider("primary", 1.0, 1, 0.01)
    breakers = {"primary": CircuitBreaker("primary", failure_threshold=5, reset_timeout_seconds=10)}
    result = ReliabilityGateway([primary], breakers).complete("q")

    assert result.route == "static_fallback"
    assert result.error is not None
    assert "primary" in result.error


def _budget_gateway(budget: float) -> tuple[ReliabilityGateway, CountingProvider, CountingProvider]:
    expensive = CountingProvider("expensive", fail_rate=0.0)
    expensive.cost_per_1k_tokens = 1.0
    cheap = CountingProvider("cheap", fail_rate=0.0)
    cheap.cost_per_1k_tokens = 0.001
    breakers = {
        "expensive": CircuitBreaker("expensive", failure_threshold=3, reset_timeout_seconds=10),
        "cheap": CircuitBreaker("cheap", failure_threshold=3, reset_timeout_seconds=10),
    }
    gateway = ReliabilityGateway([expensive, cheap], breakers, cost_budget=budget)
    return gateway, expensive, cheap


def test_cost_budget_soft_cap_prefers_the_cheap_provider() -> None:
    """Past 80% of budget the expensive provider is shed, the cheapest still serves."""
    gateway, expensive, cheap = _budget_gateway(budget=1.0)
    gateway.total_cost = 0.85

    result = gateway.complete("soft cap question")

    assert result.provider == "cheap"
    assert expensive.calls == 0
    assert cheap.calls == 1
    assert gateway.budget_skips == 1


def test_cost_budget_exhausted_falls_back_to_static() -> None:
    """At 100% the gateway pays for nothing: cache-only, otherwise static fallback."""
    gateway, expensive, cheap = _budget_gateway(budget=1.0)
    gateway.total_cost = 1.2

    result = gateway.complete("hard cap question")

    assert result.route == "static_fallback"
    assert result.error is not None
    assert "budget" in result.error
    assert expensive.calls == 0
    assert cheap.calls == 0


def test_no_cost_budget_means_no_shedding() -> None:
    gateway, expensive, _cheap = _budget_gateway(budget=1.0)
    gateway.cost_budget = None
    gateway.total_cost = 99.0

    result = gateway.complete("unbounded question")

    assert result.provider == "expensive"
    assert expensive.calls == 1
    assert gateway.budget_skips == 0


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #


def test_gateway_is_safe_under_concurrent_load() -> None:
    primary = FakeLLMProvider("primary", 0.3, 1, 0.01)
    backup = FakeLLMProvider("backup", 0.0, 1, 0.006)
    breakers = {
        "primary": CircuitBreaker("primary", failure_threshold=3, reset_timeout_seconds=0.05),
        "backup": CircuitBreaker("backup", failure_threshold=3, reset_timeout_seconds=0.05),
    }
    gateway = ReliabilityGateway([primary, backup], breakers, ResponseCache(60, 0.92))

    prompts = [f"concurrent question {index % 10}" for index in range(160)]
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(gateway.complete, prompts))

    assert len(results) == len(prompts)
    assert all(result.text for result in results)
    # Every logged transition is a real edge: no state was skipped or duplicated.
    for breaker in breakers.values():
        for entry in breaker.transition_log:
            assert entry["from"] != entry["to"]


def test_cache_survives_concurrent_readers_and_writers() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.92)

    def hammer(index: int) -> None:
        key = f"question {index % 8}"
        cache.set(key, f"answer {index % 8}")
        cache.get(key)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(hammer, range(400)))

    # set() de-duplicates by key, so the entry list stays bounded by the key space.
    assert len(cache._entries) == 8


# --------------------------------------------------------------------------- #
# Privacy over the real corpus
# --------------------------------------------------------------------------- #


def test_no_privacy_labelled_query_is_ever_stored() -> None:
    lines = (REPO_ROOT / "data" / "sample_queries.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    privacy = [r for r in records if r["expected_risk"] == "privacy"]
    assert privacy, "corpus should contain privacy-labelled queries"

    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.3)
    for record in privacy:
        assert _is_uncacheable(record["query"]), record["id"]
        cache.set(record["query"], "SENSITIVE")
        assert cache.get(record["query"]) == (None, 0.0)

    assert cache._entries == []


def test_dated_query_pairs_are_blocked_as_false_hits() -> None:
    """q7/q8 and q16/q17 differ only by year and must never serve each other."""
    lines = (REPO_ROOT / "data" / "sample_queries.jsonl").read_text(encoding="utf-8").splitlines()
    by_id = {json.loads(line)["id"]: json.loads(line)["query"] for line in lines if line.strip()}

    for first, second in (("q7", "q8"), ("q16", "q17")):
        cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.92)
        cache.set(by_id[first], "answer for the first year")
        cached, score = cache.get(by_id[second])

        assert score >= 0.92, "the pair should be similar enough to fool a threshold alone"
        assert cached is None, "false-hit detection must reject it anyway"
        assert cache.false_hit_log[-1]["reason"] == "date_or_number_mismatch"


# --------------------------------------------------------------------------- #
# SLO floor on a real (small) chaos run
# --------------------------------------------------------------------------- #


def _fast_config() -> LabConfig:
    config = load_config(REPO_ROOT / "configs" / "default.yaml").model_copy(deep=True)
    for provider in config.providers:
        provider.base_latency_ms = 1
    config.load_test.requests = 60
    return config


def test_healthy_scenario_meets_the_availability_slo() -> None:
    config = _fast_config()
    scenario = ScenarioConfig(
        name="all_healthy",
        provider_overrides={"primary": 0.0, "backup": 0.0},
    )
    metrics = run_scenario(config, load_queries(REPO_ROOT / "data" / "sample_queries.jsonl"), scenario)

    assert metrics.availability >= 0.99
    assert metrics.circuit_open_count == 0
    assert metrics.cache_hit_rate >= 0.10


def test_dead_primary_still_serves_through_the_backup() -> None:
    config = _fast_config()
    scenario = ScenarioConfig(name="primary_timeout_100", provider_overrides={"primary": 1.0})
    metrics = run_scenario(config, load_queries(REPO_ROOT / "data" / "sample_queries.jsonl"), scenario)

    assert metrics.circuit_open_count >= 1, "primary circuit must trip"
    assert metrics.availability >= 0.9
    assert metrics.fallback_successes > 0


def test_cache_disabled_scenario_records_no_hits() -> None:
    config = _fast_config()
    scenario = ScenarioConfig(
        name="no_cache_baseline",
        provider_overrides={"primary": 0.0, "backup": 0.0},
        cache_enabled=False,
    )
    metrics = run_scenario(config, load_queries(REPO_ROOT / "data" / "sample_queries.jsonl"), scenario)

    assert metrics.cache_hits == 0
    assert metrics.availability >= 0.99


def test_build_gateway_honours_the_cache_override() -> None:
    config = _fast_config()
    assert build_gateway(config, None, cache_enabled=False).cache is None
    assert build_gateway(config, None, cache_enabled=True).cache is not None


def test_provider_error_is_raised_not_swallowed() -> None:
    """Sanity check on the fixture the whole suite depends on."""
    provider = FakeLLMProvider("always_fails", 1.0, 1, 0.001)
    try:
        provider.complete("q")
    except ProviderError as exc:
        assert "always_fails" in str(exc)
    else:  # pragma: no cover - would mean the fake provider is broken
        raise AssertionError("expected ProviderError")


# --------------------------------------------------------------------------- #
# Bounded retry (opt-in; the shipped default is no retry at all)
# --------------------------------------------------------------------------- #


def test_retry_is_disabled_by_default() -> None:
    """The default gateway matches the lab spec: a provider error is not retried."""
    provider = CountingProvider("only", fail_rate=1.0)
    breakers = {"only": CircuitBreaker("only", failure_threshold=99, reset_timeout_seconds=10)}
    gateway = ReliabilityGateway([provider], breakers)

    result = gateway.complete("q")

    assert result.route == "static_fallback"
    assert provider.calls == 1
    assert gateway.retries_used == 0


def test_bounded_retry_gives_the_last_provider_a_second_chance() -> None:
    provider = CountingProvider("only", fail_rate=1.0)
    breakers = {"only": CircuitBreaker("only", failure_threshold=99, reset_timeout_seconds=10)}
    gateway = ReliabilityGateway(
        [provider], breakers, max_retries_per_request=1, retry_budget_ratio=1.0
    )

    gateway.complete("q")

    assert provider.calls == 2, "one original attempt plus one retry"
    assert gateway.retries_used == 1


def test_retry_budget_refuses_a_sustained_outage() -> None:
    """A budget of 10% means a long outage cannot multiply load without bound."""
    provider = CountingProvider("only", fail_rate=1.0)
    breakers = {"only": CircuitBreaker("only", failure_threshold=999, reset_timeout_seconds=10)}
    gateway = ReliabilityGateway(
        [provider], breakers, max_retries_per_request=1, retry_budget_ratio=0.1
    )

    for index in range(100):
        gateway.complete(f"prompt {index}")

    assert gateway.retries_used <= 10, "retries capped at 10% of traffic"
    assert gateway.retries_denied > 0, "the budget must actually refuse retries"
    assert provider.calls == 100 + gateway.retries_used


def test_open_circuit_is_never_retried() -> None:
    """Retry must not defeat the breaker: an open circuit still fails fast."""
    provider = CountingProvider("only", fail_rate=1.0)
    breakers = {"only": CircuitBreaker("only", failure_threshold=2, reset_timeout_seconds=60)}
    gateway = ReliabilityGateway(
        [provider], breakers, max_retries_per_request=3, retry_budget_ratio=1.0
    )

    for _ in range(20):
        gateway.complete("q")

    # The breaker trips after 2 recorded failures; everything after is shed, retries
    # included, so the dead provider is not hammered.
    assert provider.calls == 2

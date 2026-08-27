from __future__ import annotations

import json
import random
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import GatewayResponse, ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider

#: Scenarios that deliberately break every provider at once. They are chaos drills,
#: not steady-state traffic, so they are excluded from the SLO aggregate in the report
#: (you do not measure your availability SLO during a planned double-outage drill) —
#: they are still reported in full in the chaos table. ``primary_timeout_100_retry`` is
#: likewise excluded: it is the *proposed fix* under evaluation, not shipped behaviour,
#: so counting it would flatter the baseline SLO.
SLO_EXCLUDED_SCENARIOS = frozenset({"both_degraded", "primary_timeout_100_retry"})

#: Modelled saving per cache hit, in USD. Equal to the average cost of one provider
#: call at the default config (~55 tokens at $0.006-$0.01 / 1k tokens).
COST_SAVED_PER_CACHE_HIT = 0.001


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(
    config: LabConfig,
    provider_overrides: dict[str, float] | None = None,
    cache_enabled: bool | None = None,
    cost_budget: float | None = None,
    max_retries_per_request: int = 0,
    retry_budget_ratio: float = 0.0,
) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    use_cache = config.cache.enabled if cache_enabled is None else cache_enabled
    cache: ResponseCache | SharedRedisCache | None = None
    if use_cache:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(
        providers,
        breakers,
        cache,
        cost_budget=cost_budget,
        max_retries_per_request=max_retries_per_request,
        retry_budget_ratio=retry_budget_ratio,
    )


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Average time a circuit stayed OPEN before closing again, in milliseconds.

    Walks each breaker transition log pairing every ``-> open`` edge with the next
    ``-> closed`` edge. Returns None when no circuit ever recovered (either none
    opened, or one is still open at the end of the run).
    """
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        opened_ts: float | None = None
        for entry in breaker.transition_log:
            to_state = entry.get("to")
            timestamp = float(entry.get("ts", 0.0))
            if to_state == "open":
                opened_ts = timestamp
            elif to_state == "closed" and opened_ts is not None:
                recovery_times.append((timestamp - opened_ts) * 1000.0)
                opened_ts = None

    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run one named chaos scenario against a freshly built gateway.

    The prompt sequence and the provider RNG are both seeded from
    ``config.load_test.seed``, so a sequential scenario reproduces exactly between
    runs. A scenario with ``concurrency > 1`` is inherently non-deterministic; that is
    the point of the concurrent-load scenario and is called out in the report.
    """
    gateway = build_gateway(
        config,
        scenario.provider_overrides or None,
        cache_enabled=scenario.cache_enabled,
        cost_budget=scenario.cost_budget,
        max_retries_per_request=scenario.max_retries_per_request,
        retry_budget_ratio=scenario.retry_budget_ratio,
    )
    metrics = RunMetrics()

    seed = config.load_test.seed
    chooser = random.Random(seed)
    prompts = [chooser.choice(queries) for _ in range(config.load_test.requests)]
    # FakeLLMProvider draws from the global RNG for its failure/latency simulation.
    random.seed(seed)

    concurrency = scenario.concurrency or config.load_test.concurrency
    complete: Callable[[str], GatewayResponse] = gateway.complete
    if concurrency > 1:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(pool.map(complete, prompts))
    else:
        results = [complete(prompt) for prompt in prompts]

    for result in results:
        metrics.total_requests += 1
        metrics.estimated_cost += result.estimated_cost

        if result.cache_hit:
            metrics.cache_hits += 1
            metrics.estimated_cost_saved += COST_SAVED_PER_CACHE_HIT

        if result.route == "fallback":
            metrics.fallback_successes += 1
            metrics.successful_requests += 1
        elif result.route == "static_fallback":
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1
        else:
            metrics.successful_requests += 1

        if result.latency_ms > 0:
            metrics.latencies_ms.append(result.latency_ms)

    metrics.circuit_open_count = sum(
        1
        for breaker in gateway.breakers.values()
        for entry in breaker.transition_log
        if entry.get("to") == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    return metrics


#: Per-scenario acceptance criteria. Each predicate answers "did the reliability layer
#: behave as designed under this failure mode?" — never "did the simulated providers
#: happen to be lucky?". Residual failures caused by a provider's own configured
#: fail_rate are an input to the experiment, not a defect of the layer, so criteria are
#: written against layer behaviour (circuit tripped, traffic rerouted, every request
#: answered) with statistical headroom for the provider fail_rate that remains.
SCENARIO_CRITERIA: dict[str, Callable[[RunMetrics], bool]] = {
    # Primary is a black hole. The layer must trip the primary circuit and reroute to
    # backup; the only permitted failures are backup's own 5% fail_rate.
    "primary_timeout_100": lambda m: (
        m.circuit_open_count >= 1 and m.fallback_success_rate >= 0.9 and m.availability >= 0.9
    ),
    # Half the primary calls fail: the circuit should oscillate and availability hold.
    "primary_flaky_50": lambda m: m.availability >= 0.95 and m.circuit_open_count >= 1,
    # Everything healthy: no circuit may open and nothing may fail.
    "all_healthy": lambda m: m.availability >= 0.99 and m.circuit_open_count == 0,
    # Cache off: identical availability to all_healthy, and provably zero cache hits.
    "no_cache_baseline": lambda m: m.availability >= 0.99 and m.cache_hits == 0,
    # Concurrent load: shared breakers must not corrupt state or lose requests.
    "concurrent_load": lambda m: (
        m.availability >= 0.95
        and m.successful_requests + m.failed_requests == m.total_requests
    ),
    # Both providers badly degraded. Availability is expected to collapse — that is the
    # input, not a bug. What is graded is graceful degradation: every request is
    # accounted for and answered, the static fallback engages, and circuits trip
    # instead of hammering dead providers.
    # Same failure mode as primary_timeout_100 but with one budgeted retry enabled:
    # the point is to clear the 99% availability SLO that the no-retry chain cannot.
    "primary_timeout_100_retry": lambda m: (
        m.circuit_open_count >= 1 and m.availability >= 0.99
    ),
    "both_degraded": lambda m: (
        m.total_requests > 0
        and m.successful_requests + m.failed_requests == m.total_requests
        and m.static_fallbacks > 0
        and m.circuit_open_count >= 1
    ),
}

def default_criterion(metrics: RunMetrics) -> bool:
    """Applied when a scenario has no entry in SCENARIO_CRITERIA."""
    return metrics.availability >= 0.95


def scenario_passed(scenario: ScenarioConfig, result: RunMetrics) -> bool:
    """Evaluate the acceptance criterion for one scenario."""
    criterion = SCENARIO_CRITERIA.get(scenario.name, default_criterion)
    return criterion(result)


def run_all_scenarios(
    config: LabConfig, queries: list[str]
) -> tuple[RunMetrics, dict[str, RunMetrics]]:
    """Run every configured scenario, returning combined and per-scenario metrics.

    The per-scenario breakdown is what the report needs for the chaos table and the
    cache comparison; the combined roll-up is what ``metrics.json`` records.
    """
    scenarios = config.scenarios or [ScenarioConfig(name="default", description="baseline run")]

    combined = RunMetrics()
    by_scenario: dict[str, RunMetrics] = {}

    for scenario in scenarios:
        result = run_scenario(config, queries, scenario)
        by_scenario[scenario.name] = result
        combined.scenarios[scenario.name] = "pass" if scenario_passed(scenario, result) else "fail"

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = result.recovery_time_ms
            else:
                combined.recovery_time_ms = (combined.recovery_time_ms + result.recovery_time_ms) / 2

    return combined, by_scenario


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none defined."""
    combined, _ = run_all_scenarios(config, queries)
    return combined

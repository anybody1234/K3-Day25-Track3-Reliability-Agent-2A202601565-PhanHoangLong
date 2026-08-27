from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse

#: Served when every provider in the chain is unavailable. A degraded answer beats a
#: 500: the caller still gets a well-formed response and can retry later.
STATIC_FALLBACK_TEXT = "The service is temporarily degraded. Please try again soon."


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None
    #: Human-readable route reason including the provider that served (or refused)
    #: the request, e.g. ``"fallback:backup"``. ``route`` stays a stable enum-like
    #: token for metrics; ``route_detail`` is what the report and logs quote.
    route_detail: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers.

    Request pipeline::

        prompt -> cache lookup -> provider[0] via breaker -> provider[1] via breaker
               -> ... -> static fallback

    A provider is skipped without being called at all when its breaker is OPEN, which
    is what prevents a retry storm against an already-failing dependency.
    """

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
        cost_budget: float | None = None,
        max_retries_per_request: int = 0,
        retry_budget_ratio: float = 0.0,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache
        #: Optional spend cap. Once 80% is consumed the gateway stops paying for
        #: expensive providers and prefers the cheapest one; at 100% it serves cache
        #: or the static fallback only.
        self.cost_budget = cost_budget
        self.total_cost = 0.0
        self.budget_skips = 0

        #: Bounded retry, OFF by default so the shipped behaviour matches the lab spec
        #: exactly (a provider error falls straight through to the next route).
        #:
        #: When enabled, only the LAST provider in the chain is retried, only for a real
        #: ProviderError, and only while the global retry budget holds. A CircuitOpenError
        #: is never retried -- that is the whole point of the breaker. The budget, not the
        #: absence of retries, is what prevents a retry storm: past
        #: ``retry_budget_ratio`` of traffic, retries are refused outright.
        self.max_retries_per_request = max_retries_per_request
        self.retry_budget_ratio = retry_budget_ratio
        self.requests_seen = 0
        self.retries_used = 0
        self.retries_denied = 0

        self._lock = threading.Lock()

    def complete(self, prompt: str) -> GatewayResponse:
        """Return a reliable response, or a static fallback if every route fails."""
        with self._lock:
            self.requests_seen += 1

        # 1. Cache check -------------------------------------------------------
        if self.cache is not None:
            started = time.perf_counter()
            cached_text, score = self.cache.get(prompt)
            if cached_text is not None:
                # Real measured latency (tens of microseconds), not a hard-coded 0:
                # run_scenario only records latencies > 0, and dropping cache hits from
                # the distribution would make the with/without-cache comparison in the
                # report show no latency improvement at all.
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                return GatewayResponse(
                    text=cached_text,
                    route=f"cache_hit:{score:.2f}",
                    provider=None,
                    cache_hit=True,
                    latency_ms=elapsed_ms,
                    estimated_cost=0.0,
                    route_detail=f"cache_hit:{score:.2f}",
                )

        # 2. Provider fallback chain -------------------------------------------
        last_error: str | None = None
        cheapest = self._cheapest_provider_name()

        for index, provider in enumerate(self.providers):
            route = "primary" if index == 0 else "fallback"

            if self._skip_for_budget(provider, cheapest):
                with self._lock:
                    self.budget_skips += 1
                last_error = f"{provider.name}: skipped, cost budget exhausted"
                continue

            breaker = self.breakers[provider.name]
            is_last_route = index == len(self.providers) - 1
            attempts_left = 1 + (self.max_retries_per_request if is_last_route else 0)
            response: ProviderResponse | None = None

            while attempts_left > 0:
                attempts_left -= 1
                try:
                    response = breaker.call(provider.complete, prompt)
                    break
                except CircuitOpenError as exc:
                    # Never retried: fail fast is exactly what an open circuit is for.
                    last_error = f"{provider.name}: circuit_open: {exc}"
                    break
                except ProviderError as exc:
                    last_error = f"{provider.name}: provider_error: {exc}"
                    if attempts_left > 0 and self._claim_retry():
                        continue
                    break

            if response is None:
                continue

            if self.cache is not None:
                self.cache.set(prompt, response.text, {"provider": provider.name})
            with self._lock:
                self.total_cost += response.estimated_cost

            return GatewayResponse(
                text=response.text,
                route=route,
                provider=provider.name,
                cache_hit=False,
                latency_ms=response.latency_ms,
                estimated_cost=response.estimated_cost,
                route_detail=f"{route}:{provider.name}",
            )

        # 3. Static fallback ----------------------------------------------------
        return GatewayResponse(
            text=STATIC_FALLBACK_TEXT,
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=0.0,
            estimated_cost=0.0,
            error=last_error or "no providers available",
            route_detail="static_fallback",
        )

    def retry_stats(self) -> dict[str, object]:
        """Retry counters, for the metrics report."""
        with self._lock:
            return {
                "enabled": self.max_retries_per_request > 0,
                "max_retries_per_request": self.max_retries_per_request,
                "retry_budget_ratio": self.retry_budget_ratio,
                "requests_seen": self.requests_seen,
                "retries_used": self.retries_used,
                "retries_denied_by_budget": self.retries_denied,
            }

    def _claim_retry(self) -> bool:
        """Consume one unit of retry budget, or refuse when the budget is spent.

        The budget is a fraction of total traffic (SRE Workbook style): a burst of
        failures can be retried, a sustained outage cannot, so retries can never
        multiply load without bound.
        """
        with self._lock:
            allowance = self.retry_budget_ratio * max(1, self.requests_seen)
            if self.retries_used >= allowance:
                self.retries_denied += 1
                return False
            self.retries_used += 1
            return True

    def breaker_states(self) -> dict[str, str]:
        """Current state of every breaker, for metrics and the report."""
        return {name: breaker.state.value for name, breaker in self.breakers.items()}

    def _cheapest_provider_name(self) -> str | None:
        if not self.providers:
            return None
        return min(self.providers, key=lambda p: p.cost_per_1k_tokens).name

    def _skip_for_budget(self, provider: FakeLLMProvider, cheapest: str | None) -> bool:
        """Cost-aware routing: shed expensive providers as the budget runs out."""
        if self.cost_budget is None or self.cost_budget <= 0:
            return False
        with self._lock:
            spent_ratio = self.total_cost / self.cost_budget
        if spent_ratio >= 1.0:
            # Budget exhausted: cache-only or static fallback.
            return True
        if spent_ratio >= 0.8:
            # Soft cap: only the cheapest provider may still be paid for.
            return provider.name != cheapest
        return False

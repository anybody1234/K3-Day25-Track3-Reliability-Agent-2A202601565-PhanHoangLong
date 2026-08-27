from __future__ import annotations

import argparse
import json
from pathlib import Path

from reliability_lab.chaos import load_queries, run_all_scenarios, scenario_passed
from reliability_lab.config import load_config
from reliability_lab.metrics import RunMetrics


def _by_scenario_payload(
    config_path: str, by_scenario: dict[str, RunMetrics], config: object
) -> dict[str, object]:
    scenarios = getattr(config, "scenarios", [])
    descriptions = {s.name: s.description for s in scenarios}
    criteria_met = {
        s.name: scenario_passed(s, by_scenario[s.name])
        for s in scenarios
        if s.name in by_scenario
    }
    return {
        "config": config_path,
        "scenarios": {
            name: {
                "description": descriptions.get(name, ""),
                "passed": criteria_met.get(name),
                **metrics.to_report_dict(),
                # Raw counters + every latency sample, so the report can aggregate an
                # arbitrary subset of scenarios exactly rather than averaging ratios.
                "raw": metrics.model_dump(),
            }
            for name, metrics in by_scenario.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--out", default="reports/metrics.json")
    parser.add_argument(
        "--csv",
        default=None,
        help="CSV export path (default: --out with a .csv suffix)",
    )
    parser.add_argument(
        "--flush-cache",
        action="store_true",
        help=(
            "Clear the Redis cache prefix before running. Without it a Redis-backed run "
            "starts warm from whatever other instances left behind, which is realistic "
            "but not comparable against a cold in-memory run."
        ),
    )
    parser.add_argument(
        "--by-scenario",
        default=None,
        help="Per-scenario metrics path (default: --out with a _by_scenario.json suffix)",
    )
    args = parser.parse_args()

    out_path = Path(args.out)
    csv_path = Path(args.csv) if args.csv else out_path.with_suffix(".csv")
    by_scenario_path = (
        Path(args.by_scenario)
        if args.by_scenario
        else out_path.with_name(f"{out_path.stem}_by_scenario.json")
    )

    config = load_config(args.config)

    if args.flush_cache and config.cache.backend == "redis":
        from reliability_lab.cache import SharedRedisCache

        scratch = SharedRedisCache(
            config.cache.redis_url,
            config.cache.ttl_seconds,
            config.cache.similarity_threshold,
        )
        scratch.flush()
        scratch.close()
        print(f"flushed redis prefix rl:cache:* on {config.cache.redis_url}")

    combined, by_scenario = run_all_scenarios(config, load_queries())

    combined.write_json(out_path)
    combined.write_csv(csv_path)
    by_scenario_path.parent.mkdir(parents=True, exist_ok=True)
    by_scenario_path.write_text(
        json.dumps(_by_scenario_payload(args.config, by_scenario, config), indent=2),
        encoding="utf-8",
    )

    report = combined.to_report_dict()
    print(f"wrote {out_path}")
    print(f"wrote {csv_path}")
    print(f"wrote {by_scenario_path}")
    print()
    print(f"{'scenario':<22} {'avail':>7} {'p95_ms':>8} {'cache':>7} {'opens':>6}  status")
    print("-" * 64)
    for name, metrics in by_scenario.items():
        status = combined.scenarios.get(name, "?")
        print(
            f"{name:<22} {metrics.availability:>7.3f} {metrics.percentile(95):>8.1f} "
            f"{metrics.cache_hit_rate:>7.3f} {metrics.circuit_open_count:>6}  {status}"
        )
    print("-" * 64)
    print(
        f"{'COMBINED':<22} {combined.availability:>7.3f} {combined.percentile(95):>8.1f} "
        f"{combined.cache_hit_rate:>7.3f} {combined.circuit_open_count:>6}"
    )
    print()
    print(f"recovery_time_ms   : {report['recovery_time_ms']}")
    print(f"estimated_cost     : {report['estimated_cost']}")
    print(f"estimated_cost_save: {report['estimated_cost_saved']}")


if __name__ == "__main__":
    main()

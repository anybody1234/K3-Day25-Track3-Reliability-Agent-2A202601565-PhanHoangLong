.PHONY: all test lint typecheck run-chaos run-chaos-redis redis-keys evidence report clean docker-up docker-down

# Full submission pipeline: tests -> both chaos runs -> evidence -> report.
all: test run-chaos run-chaos-redis evidence report

test:
	pytest -q

lint:
	ruff check src tests scripts

typecheck:
	mypy src

run-chaos:
	python scripts/run_chaos.py --config configs/default.yaml --out reports/metrics.json

run-chaos-redis:
	python scripts/run_chaos.py --config configs/redis.yaml --out reports/metrics_redis.json --flush-cache

redis-keys:
	docker compose exec -T redis redis-cli --scan --pattern "rl:cache:*"

# Captures the artefacts the report embeds as proof: Redis shared-state transcript
# and the full test log.
evidence:
	python scripts/redis_evidence.py --out reports/redis_evidence.txt
	pytest -q > reports/test_output.txt 2>&1 || true

report:
	python scripts/generate_report.py --metrics reports/metrics.json --out reports/final_report.md

docker-up:
	docker compose up -d

docker-down:
	docker compose down

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache reports/metrics.json reports/final_report.md

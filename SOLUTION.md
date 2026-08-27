# Solution notes / Ghi chú bài làm

Học viên: **Phan Hoàng Long** — `2A202601565` · Day 25, Track 3.

Tài liệu này ghi lại những gì đã hiện thực, những chỗ **cố ý** lệch khỏi docstring gợi ý,
và cách chạy lại toàn bộ. Báo cáo số liệu nằm ở [`reports/final_report.md`](reports/final_report.md).

## Chạy lại toàn bộ / Reproduce everything

```bash
pip install -e ".[dev]"
docker compose up -d        # Redis cho shared cache
make all                    # test -> chaos (memory + redis) -> evidence -> report
```

Hoặc từng bước:

```bash
make test              # 56 passed + 7 xpassed, 0 failed  (Redis phải đang chạy)
make lint              # ruff: All checks passed
make typecheck         # mypy --strict: Success, no issues in 8 source files
make run-chaos         # -> reports/metrics.json | metrics.csv | metrics_by_scenario.json
make run-chaos-redis   # -> reports/metrics_redis.*   (configs/redis.yaml, backend: redis)
make evidence          # -> reports/redis_evidence.txt + reports/test_output.txt
make report            # -> reports/final_report.md
make redis-keys        # KEYS "rl:cache:*" từ container Redis
```

## Đã hiện thực

| File | Nội dung |
|---|---|
| `circuit_breaker.py` | `allow_request()`, `call()`, `record_success()`, `record_failure()`, `snapshot()`, khoá thread-safe |
| `cache.py` | `ResponseCache.similarity/get/set` (n-gram cosine + guardrail), `SharedRedisCache.get/set` + graceful degradation |
| `gateway.py` | `complete()` (cache → breaker → fallback → static), cost-aware routing, bounded retry (tắt mặc định) |
| `chaos.py` | `run_scenario()`, `calculate_recovery_time_ms()`, `run_all_scenarios()`, tiêu chí pass/fail từng kịch bản |
| `metrics.py` | `write_csv()` |
| `scripts/redis_evidence.py` | **mới** — thu bằng chứng shared state, privacy, false-hit, degradation |
| `tests/test_reliability_extras.py` | **mới** — 21 test cho no-retry-storm, concurrency, budget, privacy corpus |

## Stretch goals đã làm

- **Concurrency** — `ThreadPoolExecutor` trong `run_scenario`, kịch bản `concurrent_load` với 8 worker
  dùng chung circuit breaker (`docs/RUBRIC.md` liệt kê "concurrent load" là bằng chứng bắt buộc của mục Chaos).
- **Cost-aware routing** — `cost_budget`: quá 80% ngân sách thì chỉ provider rẻ nhất được gọi, quá 100% thì
  chỉ còn cache/static (đúng BONUS TODO trong `gateway.py`).
- **Redis graceful degradation** — mất Redis thì cache tự rơi về `ResponseCache` cục bộ, bật cờ `degraded`.
- **SLO table** — mục 3 của báo cáo, đo trên steady-state traffic.
- **Bounded retry có ngân sách** — hiện thực đầy đủ nhưng **tắt mặc định**; xem mục "Lệch khỏi spec" bên dưới.

Không làm: property-based tests bằng `hypothesis` (thêm dependency), và lưu trạng thái circuit breaker
trong Redis — cả hai được ghi lại trong "Next steps" của báo cáo.

## Những chỗ cố ý lệch khỏi docstring gợi ý

Ba chỗ dưới đây lệch có chủ ý; mỗi chỗ đều có lý do kỹ thuật và đều được ghi rõ trong báo cáo.

### 1. Cache hit trả `latency_ms` đo thật, không phải hằng `0.0`

Docstring của `gateway.complete()` gợi ý `latency=0` cho cache hit. Nhưng `run_scenario` chỉ ghi nhận
latency `> 0`, nên gán 0 sẽ khiến **toàn bộ cache hit biến mất khỏi phân phối latency** — và bảng
"Cache comparison" (mục 5, bắt buộc theo template) sẽ cho thấy P50/P95 *không đổi* khi bật cache,
tức phép so sánh trở nên vô nghĩa. Vì vậy cache hit đo thật bằng `time.perf_counter` (cỡ 0.0x ms).
`estimated_cost` vẫn là `0.0` vì thực sự không có lệnh gọi provider nào.

### 2. `opened_at` chỉ được ghi khi state thực sự đổi

Docstring gợi ý gán `opened_at = time.monotonic()` trong nhánh `elif failure_count >= failure_threshold`.
Nếu gán vô điều kiện, mỗi failure ghi nhận **trong lúc đang OPEN** sẽ reset đồng hồ và circuit
không bao giờ đạt HALF_OPEN — tức không bao giờ hồi phục. Code so state trước/sau `_transition()`
và chỉ đóng dấu thời gian trên cạnh thật. Test `test_failures_while_open_do_not_restart_the_reset_timer`
khoá hành vi này.

### 3. `route` giữ nguyên literal, tên provider nằm ở `route_detail`

Rubric yêu cầu "route reasons include provider name", nhưng `tests/test_gateway_contract.py` assert
`result.route == "fallback"` chính xác. Nên `route` giữ nguyên token ổn định cho metrics, và trường mới
`GatewayResponse.route_detail` mang `"fallback:backup"` cho log và báo cáo.

## Thay đổi cấu hình

- `all_healthy` đổi `provider_overrides` từ `{}` thành `{primary: 0.0, backup: 0.0}`. Với `{}` thì primary
  vẫn hỏng 25%, mâu thuẫn với chính mô tả "both providers healthy" và làm tiêu chí "không circuit nào mở"
  không thể đạt.
- `load_test.requests` 100 → **200**. Ở mức 100, cache hấp thụ ~60% nên chỉ còn ~40 lệnh gọi provider;
  sai số của `fail_rate = 5%` khi đó đủ lớn để làm tiêu chí pass/fail nhiễu (đo được: fallback_success_rate
  dao động 0.85–1.00 giữa các seed, trong khi trung bình là 0.963).
- Thêm `load_test.seed` và `load_test.concurrency`; thêm 4 kịch bản: `no_cache_baseline`,
  `concurrent_load`, `primary_timeout_100_retry`, `both_degraded` (tổng **7**).
- Thêm `configs/redis.yaml`. **`configs/default.yaml` vẫn để `backend: memory`** để `make run-chaos`
  của người chấm không crash khi chưa bật Docker.
- `.gitignore` bỏ hai dòng `reports/metrics.json` và `reports/final_report.md` — đó chính là hai
  deliverable bắt buộc theo README, không thể để bị loại trừ khỏi repo.
- `pyproject.toml` khai báo tường minh `[tool.ruff.lint].select` (để `make lint` cho kết quả giống nhau
  trên mọi phiên bản ruff) và `per-file-ignores` cho các file test gốc của đề — các file này được giữ
  **nguyên vẹn**, không sửa.

## Về `scripts/generate_report.py`

Script gốc ghi đè `reports/final_report.md` bằng một bản rút gọn 3 mục. Nếu viết tay báo cáo vào đúng
file đó thì lệnh `make report` của người chấm sẽ xoá sạch. Vì vậy script được viết lại để **sinh đủ 9 mục**
của `report_template.md` từ dữ liệu đo — mọi con số trong báo cáo đều đọc từ `reports/metrics*.json`
và `reports/redis_evidence.txt`, không có số nào gõ tay.

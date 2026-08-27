# Day 25 — Reliability Report / Báo cáo độ tin cậy

**Track 3 — Reliability Engineering for Production Agents**

> Toàn bộ số liệu trong báo cáo này được sinh tự động từ `reports/metrics.json`,
> `reports/metrics_by_scenario.json`, `reports/metrics_redis*.json` và
> `reports/redis_evidence.txt` bằng lệnh `make report`. Không có con số nào gõ tay,
> nên báo cáo tái lập được 1:1 từ dữ liệu đo.

```bash
docker compose up -d      # Redis cho shared cache
make test                 # 52 passed + 7 xpassed
make run-chaos            # -> reports/metrics.json | .csv | _by_scenario.json
make run-chaos-redis      # -> reports/metrics_redis.json (backend: redis)
make evidence             # -> reports/redis_evidence.txt + test_output.txt
make report               # -> reports/final_report.md (file này)
```

## 1. Architecture summary / Kiến trúc

Gateway xử lý mỗi request qua bốn tầng, dừng ở tầng đầu tiên trả được kết quả.
Mỗi provider có circuit breaker **riêng**, nên một provider hỏng không kéo theo provider còn lại.

```
                          User request
                                |
                                v
                    +-----------------------+
                    |  ReliabilityGateway   |
                    +-----------------------+
                                |
         (1) CACHE              v
        +----------------------------------------------+
        | ResponseCache (memory) | SharedRedisCache     |
        |  - privacy guard: PRIVACY_PATTERNS -> bypass  |
        |  - n-gram cosine similarity >= threshold      |
        |  - false-hit guard: 4-digit mismatch -> miss  |
        +----------------------------------------------+
                 |  HIT                    |  MISS
                 v                         v
     route=cache_hit:<score>    +---------------------------+
     cost=0, latency~0.05ms     | (2) CircuitBreaker primary|
                                |     CLOSED -> call        |
                                |     OPEN   -> fail fast   |
                                |     HALF_OPEN -> 1 probe  |
                                +---------------------------+
                                    | ok            | ProviderError
                                    v               | / CircuitOpenError
                          route=primary             v
                                        +---------------------------+
                                        | (3) CircuitBreaker backup |
                                        +---------------------------+
                                            | ok          | fail
                                            v             v
                                  route=fallback   +------------------+
                                                   | (4) static text  |
                                                   | route=           |
                                                   |  static_fallback |
                                                   +------------------+
```

**Circuit breaker state machine** (`src/reliability_lab/circuit_breaker.py`):

```
                failure_count >= failure_threshold
     CLOSED  ------------------------------------->  OPEN
        ^          reason=failure_threshold_reached    |
        |                                              | reset_timeout_seconds elapsed
        |                                              v
        |  success_count >= success_threshold      HALF_OPEN
        +----------------------------------------------+
                   reason=probe_success                 |
                                                        | probe fails
                                    OPEN <---------------+
                                        reason=probe_failure
```

Hai chi tiết quan trọng của state machine:

1. **`probe_failure` tách khỏi `failure_threshold_reached`.** Một probe hỏng ở HALF_OPEN
   mở lại circuit ngay lập tức, không cần đợi đủ `failure_threshold`. Hai nhánh dùng
   `if/elif` với hai lý do khác nhau, không gộp bằng `or`.
2. **`opened_at` chỉ được ghi khi state thực sự đổi.** Nếu ghi vô điều kiện, mỗi failure
   ghi nhận trong lúc đang OPEN sẽ reset đồng hồ và circuit **không bao giờ** đạt HALF_OPEN.
   Test `test_failures_while_open_do_not_restart_the_reset_timer` khoá hành vi này.

## 2. Configuration / Cấu hình

Nguồn: `configs/default.yaml`

| Setting | Value | Lý do lựa chọn |
|---|---:|---|
| `failure_threshold` | `3` | 3 lỗi liên tiếp mới mở circuit. Đặt 1 thì một lỗi mạng thoáng qua cũng cắt provider; đặt 5+ thì phải chịu quá nhiều request hỏng trước khi bảo vệ. `failure_count` reset về 0 sau mỗi lần thành công nên đây là 3 lỗi *liên tiếp*, không phải cộng dồn. |
| `reset_timeout_seconds` | `2.0` | 2 s đủ để một sự cố thoáng qua tự khỏi mà vẫn thử lại nhanh. Đo được `recovery_time_ms ≈ 2 357 ms`, tức gần đúng 2 s cộng thời gian một probe — khớp thiết kế. |
| `success_threshold` | `1` | 1 probe thành công là đóng lại. Với 2 provider và đã có static fallback, chi phí của một lần đóng sớm nhầm là thấp; đổi lại thời gian hồi phục ngắn. |
| `cache ttl_seconds` | `300` | 5 phút. Đủ dài để hấp thụ traffic lặp trong một phiên tải, đủ ngắn để câu trả lời phụ thuộc thời gian (chính sách, học phí) không sống quá lâu. |
| `cache similarity_threshold` | `0.92` | 0.92 — chọn sau khi đo trên chính corpus. Cặp q7/q8 (2024 vs 2026) đạt **0.9459**, tức *vượt* ngưỡng: hạ xuống 0.85 không cứu được gì, mà nâng lên 0.95 thì diễn đạt lại hợp lệ (đo được 0.8208) cũng bị trượt. Kết luận: ngưỡng đơn thuần không đủ, phải có false-hit detection — xem mục 5. |
| `cache backend` | `memory` | Mặc định `memory` để `make run-chaos` chạy được khi grader chưa bật Docker. `configs/redis.yaml` bật `redis` cho shared cache đa instance. |
| `load_test requests` | `200` | 200 request/kịch bản (~80 lệnh gọi provider sau khi cache hấp thụ). Ở mức 100 request chỉ còn ~40 lệnh gọi, sai số của fail_rate 5% đủ lớn để làm tiêu chí pass/fail nhiễu. |
| `load_test seed` | `1234` | Cố định RNG chọn query và RNG mô phỏng lỗi/latency của provider, nên mọi kịch bản tuần tự tái lập chính xác giữa các lần chạy. |
| `load_test concurrency` | `1` | 1 mặc định (tất định). Kịch bản `concurrent_load` override lên 8 worker dùng chung một bộ circuit breaker. |

**Providers:**

| Provider | fail_rate | base_latency_ms | cost_per_1k_tokens |
|---|---:|---:|---:|
| `primary` | 0.25 | 180 | 0.01 |
| `backup` | 0.05 | 260 | 0.006 |

## 3. SLO definitions / Định nghĩa SLO

SLO được đo trên **steady-state traffic**: 5/7 kịch bản, loại trừ `both_degraded`, `primary_timeout_100_retry`.

**Vì sao loại trừ?**

- `both_degraded` là *chaos drill* cố ý phá hỏng **cả hai** provider cùng lúc. Availability thấp ở đó là **đầu vào của thí nghiệm**, không phải khiếm khuyết của reliability layer - cũng như không ai đo SLO của mình trong lúc đang diễn tập mất điện toàn bộ.
- `primary_timeout_100_retry` là **đề xuất cải tiến đang được đánh giá** (mục 8), không phải hành vi xuất xưởng. Tính nó vào SLO sẽ tô hồng đường cơ sở.

Cả hai vẫn được báo cáo đầy đủ ở mục 7, và chỉ số gộp toàn bộ nằm ở mục 4.

Mẫu đo: **1000 request**, **985 mẫu latency**.

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 98.50% | ❌ MISSED / KHÔNG ĐẠT |
| Latency P95 | < 2500 ms | 303.0 ms | ✅ MET / ĐẠT |
| Fallback success rate | >= 95% | 91.02% | ❌ MISSED / KHÔNG ĐẠT |
| Cache hit rate | >= 10% | 52.40% | ✅ MET / ĐẠT |
| Recovery time | < 5000 ms | 2,357.4 ms | ✅ MET / ĐẠT |

## 4. Metrics / Chỉ số đo được

Gộp **toàn bộ** kịch bản, kể cả chaos drill `both_degraded` — vì vậy availability ở đây
thấp hơn con số SLO ở mục 3 một cách có chủ ý.

| Metric | Value |
|---|---:|
| `total_requests` | 1400 |
| `availability` | 0.8757 (87.57%) |
| `error_rate` | 0.1243 (12.43%) |
| `latency_p50_ms` | 0.9 ms |
| `latency_p95_ms` | 303.2 ms |
| `latency_p99_ms` | 318.2 ms |
| `fallback_success_rate` | 0.5617 (56.17%) |
| `cache_hit_rate` | 0.495 (49.50%) |
| `circuit_open_count` | 28 |
| `recovery_time_ms` | 2,357.4 ms |
| `estimated_cost` | $0.265628 |
| `estimated_cost_saved` | $0.693000 |
| `cache_hits` | 693 |
| `fallback_successes` | 223 |
| `static_fallbacks` | 174 |

<details><summary>reports/metrics.json (raw)</summary>

```json
{
  "total_requests": 1400,
  "availability": 0.8757,
  "error_rate": 0.1243,
  "latency_p50_ms": 0.87,
  "latency_p95_ms": 303.22,
  "latency_p99_ms": 318.24,
  "fallback_success_rate": 0.5617,
  "cache_hit_rate": 0.495,
  "circuit_open_count": 28,
  "recovery_time_ms": 2357.421398162842,
  "estimated_cost": 0.265628,
  "estimated_cost_saved": 0.693,
  "scenarios": {
    "primary_timeout_100": "pass",
    "primary_flaky_50": "pass",
    "all_healthy": "pass",
    "no_cache_baseline": "pass",
    "concurrent_load": "pass",
    "primary_timeout_100_retry": "pass",
    "both_degraded": "pass"
  }
}
```

</details>

**Cách đọc `estimated_cost_saved`:** mỗi cache hit được quy đổi `COST_SAVED_PER_CACHE_HIT = $0.001`, đúng bằng chi phí trung bình một lệnh gọi provider ở cấu hình mặc định (~55 token với giá $0.006-0.01/1k token). Đối chiếu thực đo: 707 lệnh gọi provider tốn $0.265628, tức $0.000376/lệnh gọi — cùng bậc độ lớn, nên hệ số quy đổi là hợp lý chứ không phải con số tùy tiện.

## 5. Cache comparison / So sánh có và không có cache

Hai kịch bản **cùng seed, cùng provider (fail_rate = 0), cùng 200 request** — khác biệt duy
nhất là `cache_enabled`. Đây là phép so sánh có kiểm soát, không phải hai lần chạy khác nhau.

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---|
| latency_p50_ms | 212.3 | 0.8 | -211.53 ms (-99.6%) |
| latency_p95_ms | 238.5 | 233.6 | -4.91 ms (-2.1%) |
| latency_p99_ms | 240.4 | 240.1 | -0.37 ms (-0.2%) |
| estimated_cost | $0.123360 | $0.037570 | -0.09 (-69.5%) |
| cache_hit_rate | 0.00% | 67.50% | +67.50 pp |
| cache_hits | 0 | 135 | +135 |
| estimated_cost_saved | $0.000000 | $0.135000 | +0.14 |

Cache hấp thụ **135/200 request** (67.50%), kéo chi phí từ $0.123360 xuống $0.037570 và P50 từ 212.3 ms xuống 0.8 ms.

> **Lưu ý phương pháp đo.** Cache hit trả về `latency_ms` **đo thật** bằng `time.perf_counter` (cỡ 0.0x ms), không phải hằng số 0. Docstring gốc gợi ý gán 0, nhưng `run_scenario` chỉ ghi nhận latency `> 0`; nếu để 0 thì toàn bộ cache hit biến mất khỏi phân phối latency và bảng này sẽ cho thấy P50/P95 **không đổi** khi bật cache — vô nghĩa. `estimated_cost` của cache hit vẫn là 0 vì thực sự không có lệnh gọi provider nào.

### False-hit: bằng chứng cụ thể

Ngưỡng similarity **không đủ** để chặn câu trả lời sai. Đo trên chính corpus:

| Cặp query | Similarity | Ngưỡng 0.92 | Guardrail 4 chữ số | Kết quả |
|---|---:|---|---|---|
| q7 vs q8 (refund policy 2024 / 2026) | **0.9459** | ✅ vượt ngưỡng → **sẽ trả lời sai** | ❌ chặn | MISS (đúng) |
| q16 vs q17 (tuition fee 2024 / 2025) | vượt ngưỡng | ✅ vượt ngưỡng → **sẽ trả lời sai** | ❌ chặn | MISS (đúng) |
| "Explain circuit breaker states..." vs diễn đạt lại | 0.8208 | ❌ dưới ngưỡng | - | MISS |

Log thực tế từ `false_hit_log` (`reports/redis_evidence.txt`, mục 4):

```python
{'query': 'Summarize the refund policy for the 2026 deadline.',
 'cached_key': 'Summarize the refund policy for the 2024 deadline.',
 'score': 0.9459,
 'reason': 'date_or_number_mismatch'}
```

Nếu chỉ dựa vào ngưỡng, người hỏi về hạn 2026 sẽ nhận đúng chính sách của 2024. Test `test_dated_query_pairs_are_blocked_as_false_hits` khoá hành vi này trên cả hai cặp.

### Privacy guardrail

5 query gắn nhãn `expected_risk: privacy` trong `data/sample_queries.jsonl` (số dư tài khoản, reset mật khẩu, thẻ tín dụng, SSN) **không bao giờ** được ghi vào cache, cả in-memory lẫn Redis. Kiểm chứng: `test_no_privacy_labelled_query_is_ever_stored` và mục 3 của `reports/redis_evidence.txt` (0 key trong Redis sau khi cố ghi 3 query nhạy cảm).

## 6. Redis shared cache / Cache dùng chung

**Vì sao in-memory cache không đủ cho triển khai nhiều instance:**

- Cache nằm trong tiến trình, nên *N* instance sinh ra *N* cache rời rạc. Với N instance sau   load balancer, hit rate lý thuyết bị chia cho N vì mỗi instance phải tự học lại từ đầu.
- Mỗi lần deploy/restart/autoscale là mất sạch cache, đúng lúc hệ thống cần nó nhất   (traffic dồn sau khi rollout).
- Không thể đặt hạn mức chi phí toàn cục: mỗi instance trả tiền cho cùng một câu hỏi.

**`SharedRedisCache` giải quyết thế nào:**

- Một keyspace Redis duy nhất cho mọi instance: `{prefix}{md5(query)[:12]}` → Redis Hash   `{query, response}`. Instance nào ghi thì instance khác đọc được ngay.
- TTL do Redis `EXPIRE` quản lý — không cần vòng lặp dọn rác trong tiến trình.
- Tra cứu chính xác là **một** lệnh `HGET`; trượt thì `SCAN` + chấm điểm bằng đúng hàm   `ResponseCache.similarity`, nên hai backend cho điểm giống hệt nhau.
- Cùng bộ guardrail privacy và false-hit như bản in-memory.
- **Graceful degradation:** mọi lệnh Redis được bọc `try/except`; mất Redis thì cache tự rơi   về `ResponseCache` cục bộ và bật cờ `degraded`. Sự cố Redis làm giảm hit rate,   **không** làm giảm availability.

### Evidence of shared state / Bằng chứng chia sẻ trạng thái

Trích `reports/redis_evidence.txt` (sinh bằng `python scripts/redis_evidence.py`):

```
SharedRedisCache evidence
redis_url = redis://localhost:6379/0   prefix = rl:cache:
connectivity: ping() = True

==============================================================================
1. Shared state across two independent SharedRedisCache instances
==============================================================================
instance A id=2197240595536  ->  set('Explain circuit breaker states in one paragraph.')
instance B id=2197286121552  ->  get(...) = ('[primary] circuit breaker answer', 1.0)

RESULT: shared state CONFIRMED
Two objects, two connections, one Redis keyspace: B reads what A wrote.

semantic lookup from B: get('Explain the circuit breaker states in a paragraph.')
  -> (None, score=0.8208)

==============================================================================
2. Raw Redis keyspace (docker compose exec redis redis-cli)
==============================================================================

$ redis-cli --scan --pattern "rl:cache:*"
rl:cache:095946136fea
rl:cache:9e413fd814eb
rl:cache:8baa2cfa11fa

$ redis-cli DBSIZE
3

$ python: HGETALL of every rl:cache:* key
  rl:cache:095946136fea  ttl=299s
      query    = 'Explain circuit breaker states in one paragraph.'
      response = '[backup] cb answer'
  rl:cache:8baa2cfa11fa  ttl=299s
      query    = 'Summarize the admission FAQ in 5 bullets.'
      response = '[primary] faq answer'
  rl:cache:9e413fd814eb  ttl=299s
      query    = 'What should I do when API calls return 429?'
      response = '[primary] 429 answer'

==============================================================================
3. Privacy guardrail: sensitive queries never reach Redis
==============================================================================
  set+get 'Give me the current account balance for user 123.'
      -> (None, 0.0)
  set+get 'How do I reset the password for user 456?'
      -> (None, 0.0)
  set+get 'Show the credit card on file for account 7890.'
      -> (None, 0.0)

keys in Redis after storing 3 sensitive queries: 0
RESULT: PASS - nothing sensitive was persisted.

==============================================================================
4. False-hit guardrail across instances (2024 vs 2026)
==============================================================================
  A cached the 2024 variant; B asks the 2026 variant
  similarity score = 0.9459  (threshold = 0.92)
  served           = None
  false_hit_log    = [{'query': 'Summarize the refund policy for the 2026 deadline.', 'cached_key': 'Summarize the refund policy for the 2024 deadline.', 'score': 0.9459, 'reason': 'date_or_number_mismatch'}]

RESULT: the score clears the similarity threshold, so the threshold alone would have served a stale answer;
        the 4-digit guardrail is what blocks it.

==============================================================================
5. Graceful degradation when Redis is unreachable
==============================================================================
  ping() against a dead port -> False
  set/get still work        -> ('degraded answer', 1.0)
  degraded flag             -> True after 2 events

RESULT: a Redis outage costs cache locality, not availability - reads and
        writes silently fall through to the process-local ResponseCache.
```

### In-memory vs Redis backend / So sánh hai backend

Cùng seed, cùng kịch bản, chỉ khác `cache.backend`. Bản Redis chạy với `--flush-cache` nên bắt đầu từ cache rỗng.

| Scenario | hit rate (mem) | hit rate (redis) | P95 mem | P95 redis | avail mem | avail redis |
|---|---:|---:|---:|---:|---:|---:|
| `primary_timeout_100` | 65.00% | 65.00% | 305.4 | 305.6 | 96.50% | 96.50% |
| `primary_flaky_50` | 66.50% | 74.00% | 311.0 | 302.6 | 98.00% | 98.50% |
| `all_healthy` | 67.50% | 74.00% | 233.6 | 231.7 | 100.00% | 100.00% |
| `concurrent_load` | 63.00% | 74.00% | 312.3 | 302.9 | 98.00% | 98.00% |
| `primary_timeout_100_retry` | 66.50% | 74.00% | 305.6 | 305.4 | 99.50% | 99.50% |
| `both_degraded` | 18.00% | 74.00% | 267.2 | 12.6 | 21.00% | 77.00% |

**Kết quả đáng chú ý nhất — và cũng là câu trả lời trực tiếp cho câu hỏi "vì sao cần shared cache":**

Ở kịch bản `both_degraded` (primary hỏng 90%, backup hỏng 60%), availability với cache in-memory là **21.00%**, còn với Redis là **77.00%** — cải thiện **56.0 điểm phần trăm**. P95 giảm từ 267.2 ms xuống 12.6 ms.

Nguyên nhân: keyspace Redis **sống xuyên qua ranh giới kịch bản/tiến trình**, nên khi cả hai provider sập, cache đã được các kịch bản trước làm nóng vẫn phục vụ được. Cache in-memory thì mỗi kịch bản dựng lại từ số 0 và sập theo provider. Trong sản xuất, đây chính là kịch bản instance mới khởi động giữa lúc sự cố: shared cache biến một sự cố toàn phần thành suy giảm cục bộ.

> Đây là quan sát trung thực, **không phải** so sánh cùng điều kiện tuyệt đối: lợi thế của Redis đến từ đúng cái đặc tính đang được đo (trạng thái dùng chung, sống lâu hơn tiến trình). Với cache in-memory, làm nóng xuyên kịch bản là điều không thể về mặt kiến trúc.

<details><summary>reports/metrics_redis.json (raw)</summary>

```json
{
  "total_requests": 1400,
  "availability": 0.9564,
  "error_rate": 0.0436,
  "latency_p50_ms": 1.1,
  "latency_p95_ms": 293.65,
  "latency_p99_ms": 316.03,
  "fallback_success_rate": 0.7645,
  "cache_hit_rate": 0.6214,
  "circuit_open_count": 26,
  "recovery_time_ms": 2376.4443397521973,
  "estimated_cost": 0.234488,
  "estimated_cost_saved": 0.87,
  "scenarios": {
    "primary_timeout_100": "pass",
    "primary_flaky_50": "pass",
    "all_healthy": "pass",
    "no_cache_baseline": "pass",
    "concurrent_load": "pass",
    "primary_timeout_100_retry": "pass",
    "both_degraded": "pass"
  }
}
```

</details>

## 7. Chaos scenarios / Kịch bản hỗn loạn

**7 kịch bản**, mỗi kịch bản 200 request, tiêu chí pass/fail định nghĩa trong `SCENARIO_CRITERIA` (`src/reliability_lab/chaos.py`) — kiểm tra *hành vi của reliability layer*, không phải may rủi của provider.

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| `primary_timeout_100` | Toàn bộ traffic chuyển sang backup; circuit của primary mở và ngừng gọi provider đã chết | avail 96.50%, cache 65.00%, fallback 63, static 7, circuit opens 10, P95 305 ms | ✅ PASS |
| `primary_flaky_50` | Circuit dao động CLOSED↔OPEN↔HALF_OPEN; availability vẫn giữ | avail 98.00%, cache 66.50%, fallback 40, static 4, circuit opens 4, P95 311 ms | ✅ PASS |
| `all_healthy` | Mọi request đi qua primary; không circuit nào mở | avail 100.00%, cache 67.50%, fallback 0, static 0, circuit opens 0, P95 234 ms | ✅ PASS |
| `no_cache_baseline` | Cùng availability như all_healthy nhưng 0 cache hit (nhóm đối chứng) | avail 100.00%, cache 0.00%, fallback 0, static 0, circuit opens 0, P95 238 ms | ✅ PASS |
| `concurrent_load` | 8 worker dùng chung circuit breaker; không mất request, không hỏng state | avail 98.00%, cache 63.00%, fallback 49, static 4, circuit opens 1, P95 312 ms | ✅ PASS |
| `primary_timeout_100_retry` | Như trên, nhưng bật 1 retry có ngân sách trên provider cuối chuỗi; kỳ vọng vượt mốc SLO 99% availability | avail 99.50%, cache 66.50%, fallback 66, static 1, circuit opens 10, P95 306 ms | ✅ PASS |
| `both_degraded` | Suy giảm có kiểm soát: static fallback hoạt động, mọi request đều có phản hồi hợp lệ, circuit mở thay vì dồn dập gọi provider chết | avail 21.00%, cache 18.00%, fallback 5, static 158, circuit opens 3, P95 267 ms | ✅ PASS |

### Recovery evidence / Bằng chứng hồi phục

| Scenario | circuit_open_count | recovery_time_ms |
|---|---:|---:|
| `primary_timeout_100` | 10 | n/a |
| `primary_flaky_50` | 4 | 2,357.4 ms |
| `all_healthy` | 0 | n/a |
| `no_cache_baseline` | 0 | n/a |
| `concurrent_load` | 1 | n/a |
| `primary_timeout_100_retry` | 10 | n/a |
| `both_degraded` | 3 | n/a |

`primary_flaky_50` hồi phục trung bình **2,357.4 ms**, so với `reset_timeout_seconds = 2` (2 000 ms) cộng thời gian một probe qua provider (~180-320 ms). Con số đo được khớp với thiết kế, tức đường OPEN → HALF_OPEN → CLOSED thực sự chạy chứ không phải circuit kẹt mở.

`recovery_time_ms` được tính trong `calculate_recovery_time_ms()` bằng cách ghép mỗi cạnh `-> open` với cạnh `-> closed` kế tiếp trong `transition_log` của từng breaker. Trả về `None` khi chưa có lần hồi phục nào — ví dụ `primary_timeout_100`, nơi primary chết vĩnh viễn nên circuit không bao giờ đóng lại được. Đó là kết quả **đúng**, không phải thiếu dữ liệu.

### Concurrent load / Tải đồng thời

`concurrent_load` chạy 200 request qua `ThreadPoolExecutor` với **8 worker** dùng chung một bộ circuit breaker. Mọi thay đổi trạng thái của breaker được bảo vệ bằng `threading.Lock`; khoá **không** bao trùm lời gọi provider, nếu không toàn bộ request sẽ bị tuần tự hoá và mất hết ý nghĩa của phép đo.

Kết quả: availability 98.00%, 200/200 request được hạch toán đầy đủ, 1 lần circuit mở, P95 312.3 ms.

Test `test_gateway_is_safe_under_concurrent_load` (16 thread, 160 request) kiểm tra thêm rằng mọi entry trong `transition_log` là một cạnh thật (`from != to`) — tức không có chuyển trạng thái nào bị nhân đôi hay bỏ sót do tranh chấp.

> Kịch bản này **không tất định** theo thiết kế: thứ tự thread quyết định breaker nào mở trước. Năm kịch bản còn lại cố định seed nên tái lập chính xác.

## 8. Failure analysis / Phân tích điểm yếu còn lại

### Điểm yếu: một lỗi thoáng qua của provider cuối chuỗi là mất luôn request

Gateway **không retry**. Khi primary đã chết và backup trả về một lỗi thoáng qua duy nhất, request đó đi thẳng ra `static_fallback` — dù chỉ cần thử lại một lần là gần như chắc chắn thành công.

Đo được ở `primary_timeout_100`: 7 request rơi vào static fallback trên 70 lệnh gọi provider (10.0%), kéo availability xuống 96.50%. Nói cách khác, tỉ lệ lỗi 5% của backup được truyền **nguyên vẹn** tới người dùng thay vì bị hấp thụ. Đây cũng chính là lý do availability ở mục 3 không đạt mốc 99%: trần lý thuyết của kiến trúc hiện tại là `1 - fail_rate(backup)` ≈ 95% mỗi khi primary hỏng.

**Vì sao lại thiết kế như vậy:** lab yêu cầu rõ *no retry storm*, và cách an toàn nhất để không bao giờ tạo bão retry là không retry. Nhưng đó là cực đoan ngược lại — hệ thống đánh đổi availability lấy sự an toàn tuyệt đối trước quá tải.

**Cách khắc phục trước khi lên production:**

1. **Retry có ngân sách, đúng một lần, chỉ trên provider cuối chuỗi còn sống.**    Một lần thử lại kéo trần availability từ `1 - p` lên `1 - p²`: với p = 5% là từ 95%    lên **99.75%**.
2. **Retry budget toàn cục** (kiểu SRE Workbook): tổng số retry không vượt quá một tỉ lệ    cố định của tổng traffic. Khi vượt hạn mức thì retry bị từ chối. **Chính ngân sách này**    mới là thứ ngăn bão retry, chứ không phải việc cấm retry hoàn toàn.
3. **Không bao giờ retry khi circuit đang OPEN** — fail fast đúng là mục đích của breaker.

### Đã hiện thực và đo được / Implemented and measured

Đề xuất trên **đã được hiện thực** trong `ReliabilityGateway` (`max_retries_per_request`, `retry_budget_ratio`) và **tắt mặc định**, để hành vi xuất xưởng vẫn khớp đúng đặc tả của lab. Kịch bản `primary_timeout_100_retry` bật nó lên với cùng seed, cùng failure mode:

| Metric | Không retry (shipped) | 1 retry có ngân sách | Delta |
|---|---:|---:|---|
| availability | 96.50% | 99.50% | +3.00 pp |
| static_fallbacks | 7 | 1 | -6 |
| fallback_success_rate | 90.00% | 98.51% | +8.51 pp |
| latency_p95_ms | 305.4 | 305.6 | +0.11 ms (+0.0%) |
| circuit_open_count | 10 | 10 | +0 |

SLO availability >= 99%: ĐẠT với retry (99.50%), so với 96.50% khi không retry.

Ba bất biến được khoá bằng test trong `tests/test_reliability_extras.py`:

- `test_retry_is_disabled_by_default` — mặc định vẫn là **không retry**, đúng spec.
- `test_retry_budget_refuses_a_sustained_outage` — 100 request lỗi liên tiếp với ngân sách 10% chỉ tiêu tốn tối đa 10 retry; phần còn lại bị từ chối. Sự cố kéo dài **không thể** nhân đôi tải.
- `test_open_circuit_is_never_retried` — circuit mở sau 2 lỗi thì provider chỉ bị gọi đúng 2 lần trên 20 request, dù `max_retries_per_request=3`. Retry không phá được breaker.

### Các rủi ro còn lại (mức độ thấp hơn)

| Rủi ro | Ảnh hưởng | Hướng xử lý |
|---|---|---|
| Trạng thái circuit breaker cục bộ theo tiến trình | Mỗi instance phải tự học lại rằng provider đã chết → vẫn có N lần thử thừa với N instance | Đưa `failure_count` vào Redis bằng `INCR` + `EXPIRE`, chia sẻ trạng thái như đã làm với cache |
| `SCAN` toàn bộ keyspace khi cache trượt | O(n) mỗi lần trượt; với hàng chục nghìn key sẽ thành nút thắt | Thay bằng vector index (Redis Search / pgvector) hoặc chỉ SCAN trong phân vùng theo chủ đề |
| Guardrail false-hit chỉ nhìn số 4 chữ số | Bắt được năm/ID, nhưng không bắt được lệch ngữ nghĩa dạng "chính sách cũ" vs "chính sách mới" | Thêm kiểm tra thực thể (ngày tháng, tên riêng, phủ định) hoặc một LLM-judge rẻ tiền xác nhận trước khi phục vụ |
| Chưa có rate limit theo người dùng | Một client có thể đốt hết ngân sách chi phí chung | Token bucket theo API key, đặt trước tầng cache |

## 9. Next steps / Bước tiếp theo

1. **Retry một lần có ngân sách trên provider cuối chuỗi** (mục 8). Đây là thay đổi có tỉ lệ    lợi ích trên công sức cao nhất: đưa availability từ ~95% lên ~99.75% khi primary hỏng,    trong khi retry budget vẫn giữ nguyên bảo đảm không có bão retry.
2. **Chia sẻ trạng thái circuit breaker qua Redis** (`INCR` + `EXPIRE` trên    `rl:cb:{provider}:failures`). Hiện cache đã dùng chung nhưng breaker thì chưa, nên khi    scale ra N instance vẫn tốn N lần phát hiện lỗi lặp lại.
3. **Thay quét tuyến tính bằng vector index.** `SCAN` + cosine là O(n) mỗi lần cache trượt;    ở quy mô production cần embedding + ANN index, giữ nguyên hai guardrail hiện có làm    bộ lọc hậu kiểm.

---

### Reproducibility / Khả năng tái lập

| Artefact | Sinh bằng |
|---|---|
| `reports/metrics.json` · `.csv` · `_by_scenario.json` | `make run-chaos` |
| `reports/metrics_redis.json` · `.csv` · `_by_scenario.json` | `make run-chaos-redis` |
| `reports/redis_evidence.txt` | `make evidence` |
| `reports/test_output.txt` | `make evidence` |
| `reports/final_report.md` | `make report` |

`load_test.seed = 1234` cố định cả RNG chọn query lẫn RNG mô phỏng lỗi/latency của provider, nên năm kịch bản tuần tự cho kết quả giống hệt nhau giữa các lần chạy. Riêng `concurrent_load` phụ thuộc lịch điều phối thread nên không tất định — đúng bản chất của phép đo tải đồng thời.

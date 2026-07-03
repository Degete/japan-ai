# Production Readiness — Agent Execution Service

Scope: what I would define and change to run this service reliably in production
on GCP/Kubernetes. It builds directly on the instrumentation in `README.md` and
the fixes in `DIAGNOSIS.md`.

---

## 1. SLIs / SLOs

The service is a request/response task API with a strong multi-tenant + cost
dimension, so SLIs cover **availability, latency, and cost**, sliced by
`priority` (and reported per `tenant`). All are derivable from existing metrics.

| SLI | Definition (from our metrics) | Proposed SLO (28-day window) |
| --- | ----------------------------- | ----------------------------- |
| **Availability** (success ratio) | `1 − (failed / total)` from `agent_tasks_total` | urgent ≥ 99.5 %, normal ≥ 99 %, low ≥ 95 % |
| **Latency — urgent** | p95 of `agent_task_duration_seconds{priority="urgent"}` | p95 ≤ 12 s, p99 ≤ 20 s |
| **Latency — normal** | p95 of `agent_task_duration_seconds{priority="normal"}` | p95 ≤ 20 s |
| **Queue wait — urgent** | p95 of `agent_task_queue_wait_seconds{priority="urgent"}` | p95 ≤ 3 s (admission fairness) |
| **Correctness / no silent empties** | ratio of `completed` tasks with a non-empty result | ≥ 99.9 % |
| **Cost efficiency** | `agent_cost_usd_total` per completed task (and `agent_wasted_tokens_total / agent_tokens_total`) | wasted-token ratio ≤ 3 %; alert on cost/task drift, not a hard SLO |

Notes on why these slices:
* **Priority-specific SLOs** are essential — an aggregate latency SLO would let
  the platform meet its number while starving `low` (exactly the Issue #2
  trade-off). Separate targets make the priority policy auditable.
* The mock LLM's intentional unreliability (≈10 % 500, ≈5 % 429, ≈5 % latency
  spikes) means the *dependency* is the dominant failure source. SLOs should be
  set against **our** ability to absorb that (retries, admission, deadlines),
  and the SLO math should use an **error budget** that explicitly accounts for
  the upstream's advertised reliability.

**Error budget policy:** if the urgent availability budget is >50 % burned,
freeze risky changes and prioritise reliability work (e.g. circuit breaker,
capacity). Track burn rate, not just the point-in-time ratio.

---

## 2. Alerting

Alerts are **symptom-based** (user-visible) as pages, with **cause-based**
tickets for early warning. Multi-window, multi-burn-rate where it matters to
avoid flapping.

### Paging (SLO burn — user impact)

| Alert | Condition | Why |
| ----- | --------- | --- |
| **Urgent availability burn (fast)** | error budget burning >14.4×/1h **and** >6×/6h for urgent | classic Google multi-window burn; catches acute outages |
| **Urgent latency SLO breach** | `histogram_quantile(0.95, sum by (le) (rate(agent_task_duration_seconds_bucket{priority="urgent"}[5m]))) > 12` for 10m | urgent customers are slow |
| **Task timeout surge** | `sum(rate(agent_tasks_total{status="failed"}[5m])) / sum(rate(agent_tasks_total[5m])) > 0.05` for 10m | deadline exhaustion (the Issue #1 signature) |
| **No throughput** | `sum(rate(agent_tasks_total[5m])) == 0` while ingress > 0 | wedged/deadlocked service |

### Ticketing (early warning — before users feel it)

| Alert | Condition | Catches |
| ----- | --------- | ------- |
| **Queue backlog growing** | `agent_admission_queue_depth` high & rising for 15m | approaching capacity saturation |
| **LLM retry storm** | `sum(rate(agent_llm_retries_total[5m]))` >> baseline | upstream degradation / self-amplification |
| **Rate-limiter saturation** | p95 `agent_llm_rate_limit_wait_seconds` > 1s | client-side throttle is now the bottleneck |
| **Cost anomaly** | `agent_cost_usd_total` per-task or per-tenant rate ↑ >X% WoW | runaway spend / a tenant abusing the platform |
| **Memory-leak guard** | `agent_store_entries` near its cap or process RSS trending up | store bound mis-set / real leak (Issue #4 regression) |
| **Low-priority starvation** | p95 `agent_task_queue_wait_seconds{priority="low"}` pinned at deadline | aging mis-tuned (Issue #2 mitigation regression) |
| **Cache hit-ratio collapse** | `agent_cache_events_total` hit ratio drops sharply | cache TTL/key regression → cost spike |

All alerts should link to the trace/log correlation (a burst of `failed` tasks →
one click to the offending traces and their logs).

---

## 3. Production deployment on GCP / Kubernetes

### Statefulness / scaling
* **The service must become stateless.** Today `task_store`, `_response_cache`
  and `_execution_log` live in process memory — even bounded, they are per-pod,
  so `GET /tasks/{id}` only works if it hits the same pod, and cache/state are
  not shared. Move to:
  * **Task state + results** → Redis / Memorystore (or Firestore) with TTL.
  * **Response cache** → shared Redis (keeps the Issue #4 bound *and* makes hits
    cross-pod).
  * **Audit log** → ship to logging/BigQuery, not RAM (already structured JSON).
* Once stateless, run **N replicas behind a Service**, scale with an **HPA** on
  a custom metric — `agent_admission_queue_depth` or `agent_tasks_in_progress`
  saturation is a better signal than CPU for an IO-bound LLM workload
  (Prometheus Adapter / KEDA).
* Concurrency limits (`MAX_CONCURRENT_TASKS`, per-tenant caps) become
  **per-pod**; the global LLM budget must be enforced across pods — use a
  **distributed rate limiter** (Redis token bucket) instead of the in-process
  one, otherwise total LLM QPS scales with replica count and blows the bill.

### Async execution model
* `POST /tasks` currently runs the whole pipeline **synchronously** and can
  block for up to 30 s. In production, make it **submit → 202 Accepted** and run
  the pipeline on a **queue/worker** (Pub/Sub + workers, or Cloud Tasks). Clients
  poll `GET /tasks/{id}`. This decouples request latency from LLM latency, lets
  the deadline live with the worker, and makes autoscaling clean.

### Resilience
* **Circuit breaker** around the LLM (open after N consecutive failures, serve
  fast-fail / degraded) so a bad upstream sheds load instead of amplifying it —
  the natural follow-up to the Fix #5 retry policy.
* **PodDisruptionBudget**, liveness on `/health`, readiness gated on LLM
  reachability, graceful shutdown that drains in-flight tasks.
* **Resource requests/limits** sized from observed RSS (now bounded) and CPU;
  set the store caps and worker concurrency from load tests, not guesses.

### Observability in production
* Keep the **same OTLP → Collector → Tempo/Prometheus/Loki** design, but run the
  Collector as a **DaemonSet/gateway**, enable **tail-based sampling** (keep all
  errors + slow traces, sample the rest) to control trace volume, and use
  managed backends (**Google Managed Prometheus**, Cloud Trace/Logging, or
  self-run Grafana stack). Alloy replaces Promtail for logs (already done).
* Add **exemplars** linking metric spikes to trace IDs, and **recording rules**
  for the SLO burn expressions so alerts are cheap.

### Security / multi-tenancy hardening
* AuthN/Z on `/tasks`, per-tenant **quotas and rate limits** (a tenant should
  not be able to exhaust the global LLM budget — the per-tenant cap is a start
  but needs to be cost-aware).
* Stop returning raw stack traces / internal endpoints in error responses
  (the orchestrator currently leaks `traceback` + `LLM endpoint` to the caller).
* Secrets (LLM keys) via Secret Manager / Workload Identity, not env literals.

### Config
* Move the `config.py` literals to a **ConfigMap** (+ Secret for credentials) so
  concurrency, retry, cache and aging knobs are tunable per environment without
  a rebuild.

---

### TL;DR of the highest-leverage production changes
1. Make it **stateless** (externalise task/cache/audit state).
2. Make execution **async** (submit + worker + poll).
3. **Distributed** rate limiting + circuit breaker for the LLM budget.
4. Priority-aware **HPA** on queue depth, with per-priority SLOs + burn-rate
   alerts wired to trace/log correlation.

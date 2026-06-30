# Coding Challenge: AI Agent Platform — Observability & Diagnosis

## Challenge Overview

We provide you with a **pre-built AI agent execution service** that handles agent task requests. The service is functional but has **several hidden issues** affecting reliability, performance, and cost efficiency.

Your mission:

1. **Instrument** the service with a production-grade observability stack

2. **Identify** the hidden issues using the telemetry data you've built

3. **Provide evidence** — show the actual traces, metrics, and logs that prove each issue exists

4. **Fix** the issues and demonstrate the improvement with before/after telemetry data

> **You are NOT asked to redesign the system.** Focus on making the existing system observable, then use that observability to find and fix problems — just like a real platform engineer would.

***

## AI Tool Usage Policy

**We encourage you to use AI tools** (Cursor, Claude Code, Codex, etc.) throughout this challenge — including agentic tools that can run code, capture output, and analyze results on your behalf. In a real work environment, effectively orchestrating AI to solve engineering problems is an essential skill.

We don't draw a line between "what AI can do" and "what you must do yourself." What matters is the quality of the final output — accurate diagnosis, authentic evidence, sound engineering judgment — regardless of how you got there.

### Required: AI Usage Section in README

Include a section in your `README.md` titled **"AI Tool Usage"** describing:

* Which AI tools you used and for what tasks

* How you directed/orchestrated them — what worked well, what didn't

* Any cases where AI gave incorrect results and how you caught and handled them

> **We evaluate your ability to effectively leverage AI, not whether you avoided it.** A candidate who skillfully orchestrates AI agents to accelerate the work — then produces high-quality diagnosis with authentic evidence — is exactly what we're looking for.

***

## What You Receive

A ZIP file containing:

```
agent-platform/
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── src/
│   ├── main.py              # FastAPI application entry point
│   ├── orchestrator.py       # Agent task orchestration logic
│   ├── llm_client.py         # LLM inference client with retry logic
│   ├── tool_executor.py      # Simulated tool execution
│   ├── mock_llm_server.py    # Mock LLM endpoint (unreliable by design)
│   ├── models.py             # Data models
│   └── config.py             # Configuration
└── tests/
    └── test_load.py          # Load test script (sends concurrent requests)
```

### The Service

* A FastAPI-based agent execution service

* Accepts task requests via `POST /tasks`, returns results via `GET /tasks/{task_id}`

* Each task goes through a multi-step pipeline involving LLM inference, tool execution, and response synthesis

* A mock LLM server simulates real-world unreliability (errors, latency spikes, rate limits)

* Multi-tenant: requests include a `tenant_id` and `priority`

### What's Wrong With It

The service has **several hidden production-readiness issues**.

Some are implementation bugs. Others are behavior that is logically correct but becomes a poor choice under load, over time, or in a multi-tenant environment.

We won't tell you what they are or how many there are. Your observability stack should help you discover and justify them. In some cases, telemetry alone may not be enough — you may need to combine runtime evidence with careful code inspection.

***

## Your Tasks

### Task 1: Instrument the Service (Required)

Add observability to the existing codebase. Your instrumentation should enable you to:

* **Trace** a request end-to-end through the pipeline — see what stages it passes through, how long each takes, and what happened during retries or failures

* **Measure** key operational signals — request rates, error rates, latency distributions, resource consumption — sliceable by dimensions that matter (e.g. tenant, priority, status)

* **Correlate** logs to specific requests — when something goes wrong, you should be able to find the relevant log entries for a given trace

* Expose an operational **metrics endpoint** (`GET /metrics`)

Choose whatever tools and libraries you think are appropriate. We care about the observability outcomes, not the specific stack.

### Task 2: Diagnose Issues with Evidence (Required)

Run the provided load test (`test_load.py`) against your instrumented service, then write a **diagnosis report** that:

1. **Identifies each issue** you found

2. **Shows the evidence** — include actual telemetry data:

   * Screenshots or exports of traces showing the problem

   * Metric graphs or raw metric values

   * Relevant log entries

3. **Explains the root cause** based on the evidence

4. **Describes your discovery path** — how did your telemetry lead you to this issue? We value the investigative process, not just the conclusion

5. **Proposes a fix** with expected impact

> **The quality of your evidence matters more than the number of issues found.** A well-documented diagnosis of 3 issues with clear telemetry proof is better than a vague list of 5. We will cross-check that your evidence (trace IDs, timestamps, metric values) is consistent and comes from an actual running system.

Beyond clear-cut issues, you may also encounter design decisions where reasonable engineers could disagree. If you notice any such trade-offs, we welcome your analysis — explain the competing concerns, take a position, and describe under what conditions your answer would change.

### Task 3: Fix and Verify (Required)

* Implement a fix for **at least 1** of the issues you identified

* Show **before/after comparison** using your metrics and traces

* Demonstrate that the fix actually improved the situation with data

### Task 4: Production Readiness Recommendations (Required)

Write a short document (1 page) covering:

* What SLIs/SLOs would you define for this service?

* What alerts would you set up?

* What would you change for a production deployment on GCP/Kubernetes?

***

## Technical Constraints

* **Language**: Python 3.10+

* **Runtime**: Everything must run with `docker-compose up`

* **Framework restrictions**: No high-level AI frameworks (LangChain, etc.) — but this challenge isn't about AI logic anyway

* **Time expectation**: \~6–8 hours

***

## Deliverables

Submit as a **ZIP file** or **GitHub repository**:

### 1. Instrumented Source Code

* Your modified version of the provided codebase

* Clear diff: we should be able to see what you added vs. the original

### 2. Diagnosis Report (`DIAGNOSIS.md`)

This is the **most important deliverable**. It should contain:

* Each issue identified, with:

  * **Evidence**: actual trace screenshots, metric values, log excerpts from your running system

  * **Root cause analysis**

  * **Discovery path**: how your telemetry led you to this issue

  * **Proposed fix**

* Before/after comparison for issues you fixed

* All evidence must be from real system execution (we will verify consistency)

### 3. Infrastructure Files

* Updated `docker-compose.yml` (with any observability services you added)

* Any dashboards or visualization configs

### 4. `README.md`

* How to run the instrumented system

* How to reproduce the load test

* How to view traces/metrics/logs

* Brief explanation of your observability design choices

* **AI Tool Usage** section (required — see above)

***

## Evaluation Criteria

| Category                          | Weight | What We Look For                                                                                                  |
| --------------------------------- | ------ | ----------------------------------------------------------------------------------------------------------------- |
| **Diagnosis Quality**             | 30%    | Accuracy of issue identification; quality and authenticity of evidence; depth of root cause analysis              |
| **Observability Implementation**  | 25%    | Coverage, granularity, and usefulness of instrumentation; whether it actually enables diagnosis                    |
| **Fix & Verification**            | 20%    | Correctness of fixes; before/after data showing measurable improvement                                            |
| **AI Utilization & Productivity** | 15%    | Effective orchestration of AI tools; clear documentation of AI usage; smart division of labor                      |
| **Documentation**                 | 10%    | Clear README; well-structured diagnosis report; production readiness thinking                                     |

***

## Hints

* Start by reading the existing code carefully before adding any instrumentation

* Run the load test first WITHOUT instrumentation to get a baseline feel for the service behavior

* Not all issues are bugs — some are design choices that only become problematic at scale. And not all issues are visible from code alone — your telemetry is your primary diagnostic tool

* The mock LLM server's unreliability is intentional and NOT one of the hidden issues

* Think about what a real on-call engineer would need to see at 3 AM when this service is failing

* Consider running a **sustained** load test (not just a short burst) — some issues only manifest over time

* Slice metrics in ways that help you reason about the system — aggregate numbers can hide important patterns

* Not every issue needs to be fixed. Prioritize the ones you believe matter most, and explain why

* Ask yourself whether the current design would still be acceptable at materially higher scale

***

Good luck! We look forward to seeing how you make the invisible visible.

"""Application configuration."""

LLM_SERVER_URL = "http://mock-llm:8081"
TASK_TIMEOUT_SECONDS = 30
MAX_CONCURRENT_TASKS = 5
MAX_CONCURRENT_TASKS_PER_TENANT = 3
PRIORITY_AGING_INTERVAL_SECONDS = 5.0

# LLM rate limiting
LLM_RATE_LIMIT_RPS = 10         # max LLM calls per second
LLM_RATE_LIMIT_BURST = 20       # burst capacity

# Cost tracking
TOKEN_COST_PER_1K_INPUT = 0.003    # $/1K tokens
TOKEN_COST_PER_1K_OUTPUT = 0.015   # $/1K tokens

# Retry configuration
RETRY_MAX_ATTEMPTS = 5
RETRY_BASE_DELAY = 0.5            # seconds
RETRY_BACKOFF_FACTOR = 2.0        # exponential multiplier
RETRY_MAX_ATTEMPTS_RATE_LIMIT = 3    # fewer retries for 429
RETRY_RATE_LIMIT_BASE_DELAY = 2.0    # seconds; longer base backoff for 429
RETRY_TOTAL_BACKOFF_BUDGET = 8.0     # max cumulative sleep across all retries

# Validation stage toggle (for testing / debugging)
ENABLE_VALIDATION_STAGE = False

# Cache and audit log sizes
TASK_STORE_MAX_ENTRIES = 5000
TASK_STORE_TTL_SECONDS = 3600        # 1h
RESPONSE_CACHE_MAX_ENTRIES = 2000
RESPONSE_CACHE_TTL_SECONDS = 300     # 5m
EXECUTION_LOG_MAX_ENTRIES = 1000

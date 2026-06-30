"""Application configuration."""
import os

LLM_SERVER_URL = os.getenv("LLM_SERVER_URL", "http://mock-llm:8081")
TASK_TIMEOUT_SECONDS = 30
MAX_CONCURRENT_TASKS = 5

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

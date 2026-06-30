"""Mock LLM inference server.

Simulates real-world unreliability:
  - ~10% HTTP 500 errors
  - ~5% HTTP 429 rate-limit responses
  - ~5% extreme latency (>5 s)
  - Normal latency 100 ms – 1 s

This unreliability is INTENTIONAL and is NOT one of the hidden issues.
"""

import asyncio, random
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Mock LLM Server")


class InferenceRequest(BaseModel):
    prompt: str
    max_tokens: int = 512


class InferenceResponse(BaseModel):
    text: str
    prompt_tokens: int
    completion_tokens: int
    model: str = "mock-gpt-4"


@app.post("/v1/inference", response_model=InferenceResponse)
async def inference(req: InferenceRequest):
    roll = random.random()

    # ~10% server error
    if roll < 0.10:
        raise HTTPException(status_code=500, detail="Internal LLM error")

    # ~5% rate limit
    if roll < 0.15:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    # ~5% extreme latency
    if roll < 0.20:
        await asyncio.sleep(random.uniform(5, 10))

    # Normal latency
    await asyncio.sleep(random.uniform(0.1, 1.0))

    prompt_tokens = max(1, len(req.prompt.split()) * 2)
    completion_tokens = random.randint(50, req.max_tokens)

    return InferenceResponse(
        text=f"Mock response for: {req.prompt[:80]}",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)

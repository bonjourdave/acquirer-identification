"""Model client interface. The writer depends on this, not on a vendor SDK."""

from __future__ import annotations

import time
from typing import Protocol

from pydantic import BaseModel


class LLMRequest(BaseModel):
    model: str
    system: str
    user: str
    reasoning_effort: str
    timeout_s: float


class LLMResponse(BaseModel):
    parsed_json: str  # the structured output, validated against the schema by the caller
    resolved_model: str  # what the provider says actually answered (aliases can move)
    input_tokens: int
    output_tokens: int
    latency_ms: int


class ModelClient(Protocol):
    def generate(self, request: LLMRequest, schema: type[BaseModel]) -> LLMResponse: ...


class OpenAIClient:
    """OpenAI Responses API with structured output parsed into `schema`."""

    def __init__(self, api_key: str | None = None) -> None:
        from openai import OpenAI  # imported here so offline paths never need the SDK configured

        # api_key=None falls back to OPENAI_API_KEY. Retries are owned by the writer (bounded,
        # with fallback), not hidden in the SDK.
        self._client = OpenAI(api_key=api_key, max_retries=0)

    def generate(self, request: LLMRequest, schema: type[BaseModel]) -> LLMResponse:
        start = time.monotonic()
        r = self._client.responses.parse(
            model=request.model,
            instructions=request.system,
            input=request.user,
            text_format=schema,
            reasoning={"effort": request.reasoning_effort},
            timeout=request.timeout_s,
        )
        if r.output_parsed is None:
            raise ValueError(f"model returned no parseable output (status={r.status})")
        return LLMResponse(
            parsed_json=r.output_parsed.model_dump_json(),
            resolved_model=r.model,
            input_tokens=r.usage.input_tokens if r.usage else 0,
            output_tokens=r.usage.output_tokens if r.usage else 0,
            latency_ms=int((time.monotonic() - start) * 1000),
        )

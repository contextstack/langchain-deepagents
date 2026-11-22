"""Callback-based LLM call, token, and cost accounting across parent and subagent runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.outputs import LLMResult

# Approximate USD per 1M tokens (input, output) for cost-per-correct-disposition comparisons.
# Local DGX Spark models have $0 external API cost.
MODEL_PRICING_PER_1M: dict[str, tuple[float, float]] = {
    "eval-anthropic": (3.00, 15.00),
    "ops-primary": (3.00, 15.00),
    "eval-openai": (2.50, 10.00),
    "eval-gemini": (0.30, 2.50),
    "eval-dgx-coder": (0.00, 0.00),
    "eval-dgx-reasoner": (0.00, 0.00),
}


@dataclass
class UsageStats:
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def estimate_cost_usd(self, model_alias: str) -> float:
        in_rate, out_rate = MODEL_PRICING_PER_1M.get(model_alias, (3.00, 15.00))
        return round(
            (self.prompt_tokens / 1_000_000) * in_rate
            + (self.completion_tokens / 1_000_000) * out_rate,
            6,
        )


class UsageTrackingCallback(AsyncCallbackHandler):
    """Captures exact model calls and token usage across both create_agent and create_deep_agent."""

    def __init__(self) -> None:
        super().__init__()
        self.stats = UsageStats()

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        **kwargs: Any,
    ) -> None:
        self.stats.llm_calls += 1

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        usage = (response.llm_output or {}).get("token_usage") or {}
        p_tok = int(usage.get("prompt_tokens", 0) or 0)
        c_tok = int(usage.get("completion_tokens", 0) or 0)
        t_tok = int(usage.get("total_tokens", 0) or (p_tok + c_tok))

        # LangChain >= 0.3 / v1 also attaches usage_metadata on AIMessage generations
        if t_tok == 0:
            for gen_list in response.generations:
                for gen in gen_list:
                    msg = getattr(gen, "message", None)
                    meta = getattr(msg, "usage_metadata", None) if msg is not None else None
                    if isinstance(meta, dict):
                        p_tok += int(meta.get("input_tokens", 0) or 0)
                        c_tok += int(meta.get("output_tokens", 0) or 0)
                        t_tok += int(meta.get("total_tokens", 0) or 0)

        self.stats.prompt_tokens += p_tok
        self.stats.completion_tokens += c_tok
        self.stats.total_tokens += t_tok or (p_tok + c_tok)

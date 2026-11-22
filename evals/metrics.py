"""Callback-based LLM call, token, and cost accounting across parent and subagent runs."""

from __future__ import annotations

from dataclasses import dataclass, field
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
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    requested_models: list[str] = field(default_factory=list)
    response_models: list[str] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)

    @property
    def unique_requested_models(self) -> list[str]:
        return sorted(set(self.requested_models))

    @property
    def unique_response_models(self) -> list[str]:
        return sorted(set(self.response_models))

    def estimate_cost_usd(self, model_alias: str) -> float:
        in_rate, out_rate = MODEL_PRICING_PER_1M.get(model_alias, (3.00, 15.00))
        return round(
            (self.prompt_tokens / 1_000_000) * in_rate
            + (self.completion_tokens / 1_000_000) * out_rate,
            6,
        )


class UsageTrackingCallback(AsyncCallbackHandler):
    """Captures exact model calls, tool calls, model provenance, and tokens across runs."""

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
        inv = kwargs.get("invocation_params") or {}
        req_model = inv.get("model") or inv.get("model_name")
        if not req_model and isinstance(serialized, dict):
            kw = serialized.get("kwargs") or {}
            req_model = kw.get("model") or kw.get("model_name")
        if req_model:
            self.stats.requested_models.append(str(req_model))

    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        self.stats.tool_calls += 1
        name = serialized.get("name") if isinstance(serialized, dict) else None
        if name:
            self.stats.tools_called.append(str(name))

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        llm_out = response.llm_output or {}
        usage = llm_out.get("token_usage") or {}
        p_tok = int(usage.get("prompt_tokens", 0) or 0)
        c_tok = int(usage.get("completion_tokens", 0) or 0)
        t_tok = int(usage.get("total_tokens", 0) or (p_tok + c_tok))

        resp_model = llm_out.get("model_name") or llm_out.get("model")
        if resp_model:
            self.stats.response_models.append(str(resp_model))

        # LangChain >= 0.3 / v1 also attaches usage_metadata & response_metadata on AIMessage
        for gen_list in response.generations:
            for gen in gen_list:
                msg = getattr(gen, "message", None)
                if msg is None:
                    continue
                if not resp_model:
                    rmeta = getattr(msg, "response_metadata", None)
                    if isinstance(rmeta, dict):
                        rm = rmeta.get("model_name") or rmeta.get("model")
                        if rm:
                            self.stats.response_models.append(str(rm))
                if t_tok == 0:
                    meta = getattr(msg, "usage_metadata", None)
                    if isinstance(meta, dict):
                        p_tok += int(meta.get("input_tokens", 0) or 0)
                        c_tok += int(meta.get("output_tokens", 0) or 0)
                        t_tok += int(meta.get("total_tokens", 0) or 0)

        self.stats.prompt_tokens += p_tok
        self.stats.completion_tokens += c_tok
        self.stats.total_tokens += t_tok or (p_tok + c_tok)

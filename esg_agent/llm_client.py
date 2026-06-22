"""
esg_agent.llm_client — DeepSeek LLM 客户端与 Prompt 构建
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations
import json
import logging
import os
from typing import Optional

from openai import OpenAI

logger = logging.getLogger("esg_agent")


class DeepSeekClient:
    """DeepSeek API 封装，支持 token 统计追踪"""

    BASE_URL = "https://api.deepseek.com"
    MODEL = "deepseek-chat"

    # 定价 (USD per 1M tokens)
    _PRICE_INPUT_PER_1M = 0.14
    _PRICE_OUTPUT_PER_1M = 0.28

    def __init__(self):
        self._token_stats = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "total_cost_usd": 0.0,
        }
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY environment variable not set")
        self._client = OpenAI(api_key=api_key, base_url=self.BASE_URL)

    def reset_stats(self):
        self._token_stats = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "total_cost_usd": 0.0,
        }

    @property
    def stats(self) -> dict:
        return self._token_stats.copy()

    def _accumulate(self, usage) -> None:
        if usage is None:
            return
        try:
            prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion = int(getattr(usage, "completion_tokens", 0) or 0)
            total = int(getattr(usage, "total_tokens", 0) or 0) or (prompt + completion)
        except (TypeError, ValueError):
            return
        self._token_stats["prompt_tokens"] += prompt
        self._token_stats["completion_tokens"] += completion
        self._token_stats["total_tokens"] += total
        self._token_stats["total_cost_usd"] += (
            prompt * self._PRICE_INPUT_PER_1M / 1_000_000
            + completion * self._PRICE_OUTPUT_PER_1M / 1_000_000
        )

    def chat_completion(self, system_prompt: str, user_message: str, temperature: float = 0.1,
                       max_tokens: int = 8192, response_format: dict = None) -> str:
        """调用 DeepSeek chat completion，返回 content 文本。"""
        kwargs = {
            "model": self.MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            kwargs["response_format"] = response_format

        response = self._client.chat.completions.create(**kwargs)
        self._accumulate(response.usage)
        return response.choices[0].message.content or ""

    def log_summary(self) -> str:
        s = self._token_stats
        summary = (
            f"Token usage: input {s['prompt_tokens']:,} + output {s['completion_tokens']:,} "
            f"= {s['total_tokens']:,} tokens | est ${s['total_cost_usd']:.4f}"
        )
        logger.info(summary)
        return summary

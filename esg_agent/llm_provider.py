"""
esg_agent.llm_provider — LLM 供应商抽象接口
═══════════════════════════════════════════════════════════════════════════════
统一封装不同 LLM 供应商的调用差异，支持 fallback 链。
"""

from __future__ import annotations
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from openai import OpenAI

logger = logging.getLogger("esg_agent")


# ── Token 用量数据模型 ─────────────────────────────────────

@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            total_cost_usd=self.total_cost_usd + other.total_cost_usd,
        )

    def summary(self) -> str:
        return (
            f"in={self.prompt_tokens:,} out={self.completion_tokens:,} "
            f"total={self.total_tokens:,} cost=${self.total_cost_usd:.4f}"
        )


# ── 抽象供应商 ─────────────────────────────────────────────

class BaseLLMProvider(ABC):
    """LLM 供应商抽象基类。"""

    @abstractmethod
    def complete(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.1,
        max_tokens: int = 8192,
        response_format: Optional[dict] = None,
    ) -> tuple[str, TokenUsage]:
        """
        调用 LLM 完成对话。

        Returns:
            (content_string, token_usage)
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """供应商名称（用于日志和统计）。"""
        ...


# ── DeepSeek 供应商 ────────────────────────────────────────

class DeepSeekProvider(BaseLLMProvider):
    """DeepSeek API 供应商（当前默认）。"""

    BASE_URL = "https://api.deepseek.com"
    MODEL = "deepseek-chat"

    # 定价 (USD per 1M tokens)
    PRICE_INPUT_PER_1M = 0.14
    PRICE_OUTPUT_PER_1M = 0.28

    def __init__(self, api_key: str = None):
        api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY not set")
        self._client = OpenAI(api_key=api_key, base_url=self.BASE_URL)

    @property
    def name(self) -> str:
        return "deepseek"

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.1,
        max_tokens: int = 8192,
        response_format: Optional[dict] = None,
    ) -> tuple[str, TokenUsage]:
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
        content = response.choices[0].message.content or ""

        usage = self._parse_usage(response.usage)
        return content, usage

    @classmethod
    def _parse_usage(cls, api_usage) -> TokenUsage:
        if api_usage is None:
            return TokenUsage()
        try:
            prompt = int(getattr(api_usage, "prompt_tokens", 0) or 0)
            completion = int(getattr(api_usage, "completion_tokens", 0) or 0)
            total = int(getattr(api_usage, "total_tokens", 0) or 0) or (prompt + completion)
        except (TypeError, ValueError):
            return TokenUsage()
        cost = (
            prompt * cls.PRICE_INPUT_PER_1M / 1_000_000
            + completion * cls.PRICE_OUTPUT_PER_1M / 1_000_000
        )
        return TokenUsage(prompt, completion, total, cost)


# ── OpenAI 供应商 ──────────────────────────────────────────

class OpenAIProvider(BaseLLMProvider):
    """OpenAI API 供应商（可作为 fallback）。"""

    BASE_URL = "https://api.openai.com/v1"
    MODEL = "gpt-4.1-mini"

    # 定价 (USD per 1M tokens)
    PRICE_INPUT_PER_1M = 0.15
    PRICE_OUTPUT_PER_1M = 0.60

    def __init__(self, api_key: str = None, model: str = None):
        api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        self._client = OpenAI(api_key=api_key, base_url=self.BASE_URL)
        self._model = model or self.MODEL

    @property
    def name(self) -> str:
        return f"openai({self._model})"

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.1,
        max_tokens: int = 8192,
        response_format: Optional[dict] = None,
    ) -> tuple[str, TokenUsage]:
        kwargs = {
            "model": self._model,
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
        content = response.choices[0].message.content or ""

        usage = self._parse_usage(response.usage)
        return content, usage

    @classmethod
    def _parse_usage(cls, api_usage) -> TokenUsage:
        if api_usage is None:
            return TokenUsage()
        try:
            prompt = int(getattr(api_usage, "prompt_tokens", 0) or 0)
            completion = int(getattr(api_usage, "completion_tokens", 0) or 0)
            total = int(getattr(api_usage, "total_tokens", 0) or 0) or (prompt + completion)
        except (TypeError, ValueError):
            return TokenUsage()
        cost = (
            prompt * cls.PRICE_INPUT_PER_1M / 1_000_000
            + completion * cls.PRICE_OUTPUT_PER_1M / 1_000_000
        )
        return TokenUsage(prompt, completion, total, cost)


# ── Fallback 供应商链 ─────────────────────────────────────

class FallbackProvider(BaseLLMProvider):
    """
    多供应商 fallback 链。

    依次尝试 providers 列表中的供应商，任一成功即返回。
    全部失败时抛出 RuntimeError。
    """

    def __init__(self, providers: list[BaseLLMProvider]):
        if not providers:
            raise ValueError("At least one provider required for fallback chain")
        self._providers = providers

    @property
    def name(self) -> str:
        return f"fallback[{','.join(p.name for p in self._providers)}]"

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.1,
        max_tokens: int = 8192,
        response_format: Optional[dict] = None,
    ) -> tuple[str, TokenUsage]:
        last_error = None
        for provider in self._providers:
            try:
                logger.info(f"[LLM] Trying {provider.name}...")
                content, usage = provider.complete(
                    system_prompt, user_message, temperature, max_tokens, response_format
                )
                logger.info(f"[LLM] {provider.name} succeeded ({usage.summary()})")
                return content, usage
            except Exception as e:
                last_error = e
                logger.warning(f"[LLM] {provider.name} failed: {e}")
        raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")


# ── 工厂函数 ───────────────────────────────────────────────

def create_provider(
    prefer: str = "deepseek",
    enable_fallback: bool = True,
) -> BaseLLMProvider:
    """
    创建 LLM 供应商实例。

    Args:
        prefer: 首选供应商 ("deepseek" | "openai")
        enable_fallback: 是否启用 fallback

    Returns:
        BaseLLMProvider 实例
    """
    providers: list[BaseLLMProvider] = []

    if prefer == "deepseek":
        try:
            providers.append(DeepSeekProvider())
        except RuntimeError as e:
            logger.warning(f"DeepSeek not available: {e}")

    if prefer == "openai" or (enable_fallback and not providers):
        try:
            providers.append(OpenAIProvider())
        except RuntimeError as e:
            logger.warning(f"OpenAI not available: {e}")

    if not enable_fallback and prefer == "deepseek":
        try:
            providers.append(DeepSeekProvider())
        except RuntimeError:
            pass

    # Fallback: 如果首选不可用，尝试另一个
    if enable_fallback and prefer == "deepseek":
        try:
            providers.append(OpenAIProvider())
        except RuntimeError:
            pass
    elif enable_fallback and prefer == "openai":
        try:
            providers.append(DeepSeekProvider())
        except RuntimeError:
            pass

    if not providers:
        raise RuntimeError(
            "No LLM provider available. Set DEEPSEEK_API_KEY or OPENAI_API_KEY."
        )

    if len(providers) == 1:
        return providers[0]
    return FallbackProvider(providers)

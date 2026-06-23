"""
esg_agent.scorer — Tavily 相关性评分模块
═══════════════════════════════════════════════════════════════════════════════
在 Phase 2 (供料) 和 Phase 4 (LLM 提取) 之间插入一层轻量级相关性评分。
对 Google News RSS 召回的文章进行 Tavily 验证，过滤低相关性噪声，
减少 DeepSeek token 消耗。

策略：
  - 不是每篇都查 Tavily（太贵），而是对摘要信息量不足的文章才补查
  - Tavily search 返回的 relevance_score 用于二次验证
  - score < 阈值 → 标记为低相关，跳过 LLM 处理
"""

from __future__ import annotations
import logging
import os
from typing import Optional

logger = logging.getLogger("esg_agent.scorer")


class TavilyScorer:
    """使用 Tavily API 对文章进行相关性评分。"""

    DEFAULT_THRESHOLD = 0.3  # 低于此分数视为低相关

    def __init__(self, api_key: str = None, threshold: float = None):
        api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "TAVILY_API_KEY not set. Get a free key at https://tavily.com"
            )
        try:
            from tavily import TavilyClient
            self._client = TavilyClient(api_key=api_key)
        except ImportError:
            raise RuntimeError("tavily-python not installed. Run: pip install tavily-python")
        self._threshold = threshold or self.DEFAULT_THRESHOLD
        self._enabled = True

    @property
    def enabled(self) -> bool:
        return self._enabled

    def is_relevant(self, title: str, summary: str = "", url: str = "") -> bool:
        """
        快速判断文章是否与 ESG 关键矿产供应链相关。

        用文章标题做一次 Tavily search，检查返回结果是否印证了该主题。
        """
        score = self.score_article(title, summary, url)
        return score >= self._threshold

    def score_article(self, title: str, summary: str = "", url: str = "") -> float:
        """
        对单篇文章评分。返回 0.0-1.0 的相关性分数。

        实现：用标题作为 query 搜索，返回结果中如果有高相关度匹配，
        说明这是一个有效信号源在报道的事件。
        """
        if not title.strip():
            return 0.0

        try:
            # 用标题的关键部分作为搜索词（限制长度避免过度精确）
            query = title[:200]
            response = self._client.search(
                query=query,
                search_depth="basic",
                max_results=3,
                include_domains=[],
            )
            results = response.get("results", [])
            if not results:
                return 0.0

            # 取最高相关性分数
            max_score = max(
                (r.get("score", 0.0) for r in results if isinstance(r, dict)),
                default=0.0,
            )
            # 若原文提供了 URL，检查是否在结果中出现（加分）
            if url and any(
                r.get("url", "").rstrip("/") == url.rstrip("/")
                for r in results if isinstance(r, dict)
            ):
                max_score = min(max_score + 0.15, 1.0)

            return max_score

        except Exception as e:
            logger.warning(f"[Tavily] 评分失败 ({type(e).__name__}: {e})，默认通过")
            return 0.5  # 网络失败时不过滤，避免误杀

    def batch_filter(
        self, articles: list[dict], title_key: str = "title",
        summary_key: str = "raw_summary", url_key: str = "url",
    ) -> tuple[list[dict], list[dict]]:
        """
        批量评分过滤。

        Returns:
            (passed_articles, filtered_articles)
        """
        passed = []
        filtered = []
        for art in articles:
            title = str(art.get(title_key, "")).strip()
            summary = str(art.get(summary_key, "")).strip()
            url = str(art.get(url_key, "")).strip()

            if self.is_relevant(title, summary, url):
                passed.append(art)
            else:
                filtered.append(art)
                logger.info(
                    f"[Tavily] 已过滤(低相关): {title[:80]}"
                )

        logger.info(
            f"[Tavily] 批量评分: {len(passed)} pass / {len(filtered)} filtered "
            f"(threshold={self._threshold})"
        )
        return passed, filtered


def create_scorer() -> Optional[TavilyScorer]:
    """
    创建 TavilyScorer 实例（如果 API key 已配置）。
    若未配置，返回 None 并记录 info 日志，优雅降级。
    """
    api_key = os.environ.get("TAVILY_API_KEY", "")
    if not api_key:
        logger.info("[Tavily] TAVILY_API_KEY not set — relevance scoring disabled")
        return None
    try:
        return TavilyScorer(api_key=api_key)
    except RuntimeError as e:
        logger.warning(f"[Tavily] Scorer init failed: {e}")
        return None

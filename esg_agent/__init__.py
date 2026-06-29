"""
ESG 智能监控 Agent 模块 v2。

模块化架构：
  esg_agent/
  ├── config.py         — AgentConfig, QueryItem, NewsArticle, 常量
  ├── fetchers.py       — ContentExtractor, NewsFetcher, resolve_news_url, strip_html
  ├── filters.py        — EntityFilter (实体校验)
  ├── llm_client.py     — DeepSeekClient (LLM API 封装)
  ├── deduplication.py  — JaccardMerger, LLMGlobalConvergence (去重合并)
  ├── reporters.py      — MarkdownReportWriter, DingTalkPusher, NotionPusher
  └── validators.py     — Pydantic 配置验证 (ConfigSchema, validate_config)
"""

__version__ = "2.0.0"

from esg_agent.config import (
    AgentConfig, QueryItem, NewsArticle,
    FETCH_HEADERS, DINGTALK_WEBHOOK_URL,
)

from esg_agent.fetchers import (
    ContentExtractor, NewsFetcher, resolve_news_url, strip_html,
)

from esg_agent.filters import EntityFilter
from esg_agent.deduplication import JaccardMerger, LLMGlobalConvergence
from esg_agent.validators import ConfigSchema, validate_config, validate_config_or_warn

__all__ = [
    "AgentConfig",
    "QueryItem",
    "NewsArticle",
    "FETCH_HEADERS",
    "DINGTALK_WEBHOOK_URL",
    "ContentExtractor",
    "NewsFetcher",
    "resolve_news_url",
    "strip_html",
    "EntityFilter",
    "JaccardMerger",
    "LLMGlobalConvergence",
    "ConfigSchema",
    "validate_config",
    "validate_config_or_warn",
]

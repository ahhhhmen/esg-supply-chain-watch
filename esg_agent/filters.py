"""
esg_agent.filters — 实体校验与内容过滤
"""

from __future__ import annotations
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import NewsArticle


# ── 垃圾域名/关键词黑名单（RSS 噪声源） ──────────────────────
_SPAM_KEYWORDS = [
    "极速时时彩", "体育官网", "BBIN体育", "博彩", "赌博", "彩票",
    "casino", "poker", "slots", "betting", "bookmaker",
    "成人", "色情", "porn", "xxx ", "escort",
    "伟哥", "viagra", "cialis",
    "url=", "网址：",
    "crack", "keygen", "warez", "torrent",
]
_SPAM_DOMAINS: list[str] = []

_SPAM_RE = re.compile(
    "|".join(re.escape(kw) for kw in _SPAM_KEYWORDS),
    re.IGNORECASE,
)


class EntityFilter:
    """验证新闻标题或正文摘要中是否真实出现目标公司名称。

    Regex 边界匹配 + 智能大小写敏感策略 + CJK 专项处理
    外加垃圾内容黑名单过滤。
    """

    _PATTERN_CACHE: dict[str, re.Pattern] = {}
    _CJK_RE = re.compile(r"[一-鿿぀-ヿ가-힯]")

    # ── 公开接口 ──────────────────────────────────────────────

    @classmethod
    def is_spam(cls, title: str = "", url: str = "", summary: str = "") -> bool:
        """快速判断文章是否为垃圾/spam。"""
        haystack = f"{title} {url} {summary}"
        if _SPAM_RE.search(haystack):
            return True
        # 检查域名
        for domain in _SPAM_DOMAINS:
            if domain.lower() in url.lower():
                return True
        return False

    @classmethod
    def passes(cls, article: "NewsArticle", name_zh: str, name_en: str) -> bool:
        haystack = article.title + " " + article.raw_summary
        return cls._match(name_zh, haystack) or cls._match(name_en, haystack)

    # ── 内部方法 ──────────────────────────────────────────────

    @classmethod
    def _is_strict_case(cls, alias: str) -> bool:
        return alias.isupper() and alias.isascii() and len(alias) <= 5

    @classmethod
    def _has_cjk(cls, alias: str) -> bool:
        return bool(cls._CJK_RE.search(alias))

    @classmethod
    def _build_pattern(cls, alias: str) -> re.Pattern:
        is_cjk = cls._has_cjk(alias)
        is_strict = (not is_cjk) and cls._is_strict_case(alias)
        cache_key = f"{alias}|{int(is_strict)}|{int(is_cjk)}"

        if cache_key not in cls._PATTERN_CACHE:
            escaped = re.escape(alias)
            if is_cjk:
                pattern = re.compile(escaped, re.IGNORECASE)
            elif is_strict:
                pattern = re.compile(r"\b" + escaped + r"\b")
            else:
                pattern = re.compile(r"\b" + escaped + r"\b", re.IGNORECASE)
            cls._PATTERN_CACHE[cache_key] = pattern

        return cls._PATTERN_CACHE[cache_key]

    @classmethod
    def _match(cls, alias: str, haystack: str) -> bool:
        if not alias:
            return False
        return bool(cls._build_pattern(alias).search(haystack))

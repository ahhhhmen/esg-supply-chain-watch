#!/usr/bin/env python3
"""
ESG 情报监控智能体 v9 — 双频动态播报 + 四重标签审计
═══════════════════════════════════════════════════════════════════════════════
架构说明
────────
• AgentConfig.from_yaml()        — 读取多轨矩阵 + 双频主题配置
• AgentConfig.build_query_tasks(mode) — 按 mode=daily|weekly 生成搜索任务
• EntityFilter.passes()          — 实体出现校验
• NewsFetcher                    — 统一 RSS 抓取
• ContentExtractor               — 深度正文抓取
• ESGIntelligenceAgent.process_intelligence_with_llm() — DeepSeek 语义降噪 + 四重标签审计
• MarkdownReportWriter           — 按风险主题透视的报告生成
═══════════════════════════════════════════════════════════════════════════════
v9 升级
───────
1. 双频动态播报机制：--mode=daily（日常舆情） / --mode=weekly（宏观政策）
2. 地缘多语种通用轨：按公司名 + 主题关键词（多语种 OR 组合）定向 Google News
3. 四重标签审计：机构预警 / 政策前沿 / 市场准入预警 / 供应链断裂预警
4. GitHub Actions 双频 Cron 剧本
"""

import argparse
import html
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime as email_parse_date
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote, parse_qs, urlparse

import pandas as pd
import requests
import yaml
from bs4 import BeautifulSoup
from openai import OpenAI

from sourcing_engine import SourcingEngine
from backend.utils.references import clean_title, normalize_url, extract_domain_name

# ─────────────────────────────────────────────────────────
# 日志
# ─────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("esg_agent")

# ─────────────────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────────────────

# 中文轨道使用 name_zh 搜索，其他轨道使用 name_en
_GEO_CN_LANGS = {"zh-CN"}

FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ── 钉钉系统报警 Webhook ────────────────────────────────
# 填写真实 Webhook URL 后生效，为空时仅在控制台打印 ERROR 日志
DINGTALK_WEBHOOK_URL = ""


# ─────────────────────────────────────────────────────────
# 数据模型
# ─────────────────────────────────────────────────────────

@dataclass
class QueryItem:
    """单条搜索任务（v9：基于 URL 模板 + 双频主题关键词）"""
    url: str                 # 完整 RSS 请求 URL
    company_name_zh: str     # 搜索时对应的企业中文名
    company_name_en: str     # 搜索时对应的企业英文名
    track_label: str         # 轨道标签
    lang: str                # 语言标签
    topic_category: str      # 主题类别（如 "劳工权益与罢工"）


@dataclass
class NewsArticle:
    title: str = ""
    date: str = ""
    source: str = ""
    url: str = ""
    description: str = ""
    company_name_zh: str = ""
    company_name_en: str = ""
    track_label: str = ""
    lang: str = ""
    topic_category: str = ""
    parsed_date: Optional[datetime] = None
    raw_summary: str = ""


@dataclass
class AgentConfig:
    """
    v9 双频矩阵式配置。

    从 config.yaml 解析：
      - companies: list[dict]  企业名单
      - intelligence_tracks:   三大抓取轨道
      - topics:                双频主题矩阵 (daily / weekly)
      - days_limit: int        时间窗口
    """
    companies: list[dict] = field(default_factory=list)
    geographical_tracks: list[dict] = field(default_factory=list)
    premium_company_tracks: list[dict] = field(default_factory=list)
    premium_global_tracks: list[dict] = field(default_factory=list)
    daily_topics: list[dict] = field(default_factory=list)
    weekly_topics: list[dict] = field(default_factory=list)
    days_limit: int = 14

    # ── 工厂方法 ──────────────────────────────────────────

    @classmethod
    def from_yaml(cls, path: str = None) -> "AgentConfig":
        if path is None:
            path = str(Path(__file__).parent / "config.yaml")
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        tracks = raw.get("intelligence_tracks", {})
        topics = raw.get("topics", {})
        return cls(
            companies=raw.get("companies", []),
            geographical_tracks=tracks.get("geographical_tracks", []),
            premium_company_tracks=tracks.get("premium_company_tracks", []),
            premium_global_tracks=tracks.get("premium_global_tracks", []),
            daily_topics=topics.get("daily", []),
            weekly_topics=topics.get("weekly", []),
            days_limit=raw.get("days_limit", 14),
        )

    # ── 企业显示名 ──────────────────────────────────────

    def get_company_display_name(self, company: dict) -> str:
        zh = company.get("name_zh", "")
        en = company.get("name_en", "")
        return f"{zh} | {en}" if zh and en else (zh or en)

    def get_all_company_display_names(self) -> list[str]:
        return [self.get_company_display_name(c) for c in self.companies]

    # ── 主题关键词辅助方法 ──────────────────────────────

    @staticmethod
    def _get_keywords_for_lang(topic: dict, lang: str) -> list[str]:
        """从 topic 的 keywords 字典中获取指定语种的关键词列表。"""
        kw = topic.get("keywords", {})
        return kw.get(lang, []) or kw.get("en-US", [])

    @staticmethod
    def _build_keyword_query(keywords: list[str]) -> str:
        """将关键词列表组装为 OR 查询片段，如: ("strike" OR "labor" OR ...)。"""
        if not keywords:
            return ""
        if len(keywords) == 1:
            return f'"{keywords[0]}"'
        parts = [f'"{kw}"' for kw in keywords]
        return "(" + " OR ".join(parts) + ")"

    # ── 查询任务构建（双频动态路由） ──────────────────────

    def build_query_tasks(self, mode: str = "daily") -> list["QueryItem"]:
        """
        按 mode 参数生成 QueryItem 列表：

        mode=daily:
          - 轨道 1（地缘通用轨）：每企业 × 每语种 × 每日主题关键词
          - 轨道 2（BHRRC 定向）：每企业定向抓取
          - 跳过轨道 3（EFRAG 宏观政策）

        mode=weekly:
          - 轨道 1（地缘通用轨）：每企业 × 每语种 × (每日 + 周报) 主题关键词
          - 轨道 2（BHRRC 定向）：每企业定向抓取
          - 轨道 3（EFRAG 宏观政策）：全局抓取
        """
        items: list[QueryItem] = []

        # 确定主题集
        if mode == "weekly":
            active_topics = self.daily_topics + self.weekly_topics
        else:
            active_topics = self.daily_topics

        logger.info(
            f"Building query tasks for mode={mode}: "
            f"{len(active_topics)} topics, {len(self.companies)} companies, "
            f"{len(self.geographical_tracks)} geo tracks"
        )

        # ── 轨道 1：地缘多语种通用新闻网（公司 + 主题关键词） ──
        for company in self.companies:
            name_zh = company.get("name_zh", "")
            name_en = company.get("name_en", "")

            for geo in self.geographical_tracks:
                lang = geo.get("lang", "en-US")
                url_template = geo.get("url_template", "")
                lang_label = geo.get("lang_label", lang)
                if not url_template:
                    continue

                # 选择对应语言的公司名
                search_term = name_zh if lang in _GEO_CN_LANGS else name_en
                if not search_term:
                    continue

                # 收集该语言下所有主题的关键词
                for topic in active_topics:
                    category = topic.get("category", "")
                    keywords = self._get_keywords_for_lang(topic, lang)
                    if not keywords:
                        continue

                    kw_query = self._build_keyword_query(keywords)
                    # 组装完整查询: "公司名" (关键词1 OR 关键词2 ...) when:14d
                    full_query = f'"{search_term}" {kw_query} when:14d'
                    query_encoded = quote(full_query)
                    final_url = url_template.replace("{query}", query_encoded)

                    items.append(QueryItem(
                        url=final_url,
                        company_name_zh=name_zh,
                        company_name_en=name_en,
                        track_label=f"地理新闻 ({lang_label})",
                        lang=lang,
                        topic_category=category,
                    ))

        # ── 轨道 2：定向高风险机构预警（BHRRC） ───────────
        for company in self.companies:
            for pt in self.premium_company_tracks:
                url_template = pt.get("url_template", "")
                name_field = pt.get("company_name_field", "name_en")
                company_name_val = company.get(name_field, "")
                track_label = pt.get("track_label", pt.get("source", "预警"))
                if not company_name_val or not url_template:
                    continue
                final_url = url_template.replace("{company_name}", quote(company_name_val))
                items.append(QueryItem(
                    url=final_url,
                    company_name_zh=company.get("name_zh", ""),
                    company_name_en=company.get("name_en", ""),
                    track_label=track_label,
                    lang="en-US",
                    topic_category="机构预警",
                ))

        # ── 轨道 3：全球宏观合规政策前沿（仅 weekly） ────
        if mode == "weekly":
            for pt in self.premium_global_tracks:
                url = pt.get("url", "")
                track_label = pt.get("track_label", pt.get("source", "政策前沿"))
                if not url:
                    continue
                items.append(QueryItem(
                    url=url,
                    company_name_zh="",
                    company_name_en="",
                    track_label=track_label,
                    lang="en-US",
                    topic_category="宏观政策",
                ))
            logger.info(f"  Weekly mode: enabled {len(self.premium_global_tracks)} premium global track(s)")
        else:
            logger.info("  Daily mode: skipping premium global tracks (EFRAG)")

        logger.info(f"Total query tasks generated: {len(items)}")
        return items


# ─────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────

def strip_html(raw: str) -> str:
    if not raw:
        return ""
    unescaped = html.unescape(raw)
    try:
        text = BeautifulSoup(unescaped, "html.parser").get_text(separator=" ", strip=True)
    except Exception:
        text = re.sub(r"<[^>]+>", "", unescaped)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_rss_date(date_str: str) -> Optional[datetime]:
    if not date_str:
        return None
    try:
        return email_parse_date(date_str)
    except Exception:
        try:
            return datetime.strptime(date_str[:25], "%a, %d %b %Y %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except Exception:
            return None


def fmt_date(raw) -> str:
    if isinstance(raw, datetime):
        return raw.strftime("%Y-%m-%d")
    try:
        return pd.to_datetime(str(raw), utc=True).strftime("%Y-%m-%d")
    except Exception:
        return str(raw)[:10]


# ─────────────────────────────────────────────────────────
# 实体出现校验器
# ─────────────────────────────────────────────────────────

class EntityFilter:
    """
    验证新闻标题或正文摘要中是否真实出现目标公司名称。

    Regex 边界匹配 + 智能大小写敏感策略 + CJK 专项处理
    """

    _PATTERN_CACHE: dict[str, re.Pattern] = {}
    _CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

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

    @classmethod
    def passes(cls, article: "NewsArticle", name_zh: str, name_en: str) -> bool:
        haystack = article.title + " " + article.raw_summary
        return cls._match(name_zh, haystack) or cls._match(name_en, haystack)


# ─────────────────────────────────────────────────────────
# 内容提取器
# ─────────────────────────────────────────────────────────

class ContentExtractor:
    """向新闻原始 URL 发送 GET 请求，提取正文前 200 字纯文本。"""

    TIMEOUT = 5
    MAX_CHARS = 200
    _SEMANTIC_RE = re.compile(r"article|content|post|story|body|main", re.I)

    @classmethod
    def extract(cls, url: str) -> str:
        real_url = cls._unwrap_redirect(url)
        try:
            resp = requests.get(
                real_url, headers=FETCH_HEADERS, timeout=cls.TIMEOUT, allow_redirects=True,
            )
            if resp.status_code != 200:
                return ""
            return cls._parse_body(resp.text)
        except Exception as exc:
            logger.debug(f"ContentExtractor failed [{real_url[:70]}]: {exc}")
            return ""

    @classmethod
    def _unwrap_redirect(cls, url: str) -> str:
        if "apiclick.aspx" in url:
            qs = parse_qs(urlparse(url).query)
            inner = qs.get("url", [None])[0]
            if inner:
                return unquote(inner)
        return url

    @classmethod
    def _parse_body(cls, html_text: str) -> str:
        soup = BeautifulSoup(html_text, "html.parser")
        for noise in soup.find_all(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
            noise.decompose()
        container = (
            soup.find("article")
            or soup.find(["div", "section", "main"], class_=cls._SEMANTIC_RE)
            or soup.find(["div", "section", "main"], id=cls._SEMANTIC_RE)
            or soup
        )
        return cls._collect_paragraphs(container)

    @classmethod
    def _collect_paragraphs(cls, container) -> str:
        paragraphs = container.find_all("p")
        texts: list[str] = []
        total = 0
        for p in paragraphs:
            t = re.sub(r"\s+", " ", p.get_text(separator=" ", strip=True)).strip()
            if len(t) > 15:
                texts.append(t)
                total += len(t)
            if total >= cls.MAX_CHARS:
                break
        return " ".join(texts)[:cls.MAX_CHARS]


# ─────────────────────────────────────────────────────────
# RSS 抓取器
# ─────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────
# URL 解密还原：Google News → 真实源站直链
# ─────────────────────────────────────────────────────────

def resolve_news_url(url: str, timeout: int = 5) -> str:
    """将 Google News 重定向链接还原为源站直链。

    策略（v3 — requests.head 轻量解析）：
    1. 非 Google News 链接直接原样返回。
    2. 使用 HEAD + allow_redirects 跟随完整重定向链，获取最终真实 URL。
    3. 校验：最终 URL 必须与原始 URL 不同、不含 google.com、以 http 开头。
    4. 超时/网络异常时回退到原始链接。
    """
    if not url or "news.google.com" not in url:
        return url
    try:
        resp = requests.head(
            url,
            headers=FETCH_HEADERS,
            allow_redirects=True,
            timeout=timeout,
        )
        final_url = resp.url
        if (
            final_url != url
            and "google.com" not in final_url
            and len(final_url) > 15
            and final_url.lower().startswith("http")
        ):
            logger.debug(f"URL resolved: {url[:60]}... -> {final_url[:80]}...")
            return final_url
        else:
            logger.debug(f"URL NOT resolved (no redirect): {url[:60]}...")
    except Exception as exc:
        logger.debug(f"URL resolution failed [{url[:60]}...]: {exc}")
    return url


class NewsFetcher:
    """v9 统一 RSS 抓取器。"""

    TIMEOUT = 20
    MAX_RESULTS = 8

    @classmethod
    def fetch(cls, url: str) -> list[dict]:
        articles: list[dict] = []
        try:
            resp = requests.get(url, headers=FETCH_HEADERS, timeout=cls.TIMEOUT)
            if resp.status_code != 200:
                logger.debug(f"RSS fetch returned {resp.status_code}: {url[:80]}")
                return articles
            for item_xml in re.findall(r"<item>(.*?)</item>", resp.text, re.DOTALL)[:cls.MAX_RESULTS]:
                parsed = cls._parse_item(item_xml)
                if parsed:
                    parsed["description"] = strip_html(parsed.get("description", ""))
                    # 将 Google News 加密链接还原为源站直链
                    parsed["url"] = resolve_news_url(parsed["url"])
                    articles.append(parsed)
        except Exception as exc:
            logger.debug(f"RSS fetch failed [{url[:60]}]: {exc}")
        return articles

    @staticmethod
    def _parse_item(item_xml: str) -> Optional[dict]:
        t  = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item_xml)
        l  = re.search(r"<link>(.*?)</link>", item_xml)
        d  = re.search(r"<pubDate>(.*?)</pubDate>", item_xml)
        de = re.search(r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>", item_xml)
        s  = re.search(r'<source[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</source>', item_xml)

        title = (t.group(1) if t else "").strip()
        link  = (l.group(1) if l else "").strip()
        if not title or not link:
            return None

        return {
            "title":       title,
            "date":        (d.group(1) if d else "").strip(),
            "source":      (s.group(1) if s else "Unknown").strip(),
            "url":         link,
            "description": (de.group(1) if de else "").strip(),
        }


# ─────────────────────────────────────────────────────────
# Markdown 报告生成器
# ─────────────────────────────────────────────────────────

class MarkdownReportWriter:
    """
    层级结构（v9 — 按风险标签聚合）：
      ### 【供应链断裂预警】
        > **[企业名称]** — {title}
        > 💡 **高管洞察**: {insight}
        > 📅 {date} 📰 {source} 🔗 [阅读原文]({url})
        > ---
    """

    # 高管阅读优先级排序 — 数值越小优先级越高
    TAG_PRIORITY: dict[str, int] = {
        "【供应链断裂预警】": 1,
        "【地缘政治预警】":   2,
        "【市场准入预警】":   3,
        "【政策前沿】":       4,
        "【机构预警】":       5,
    }
    FALLBACK_TAG = "【日常运营风险】"

    def __init__(self, intelligence_data: list[dict], config: Optional[AgentConfig] = None, mode: str = "daily"):
        self.data = intelligence_data or []
        self.config = config
        self.mode = mode
        self.df = pd.DataFrame(self.data) if self.data else pd.DataFrame()

    # ── 标签聚合辅助 ──────────────────────────────────

    @classmethod
    def _extract_tags(cls, row) -> list[str]:
        """从一行数据中提取 tags 列表，兼容数组和字符串格式。"""
        tags_val = row.get("tags", row.get("tag", ""))
        if isinstance(tags_val, list):
            return [str(t).strip() for t in tags_val if str(t).strip()]
        if isinstance(tags_val, str) and tags_val.strip():
            # 空格或逗号分隔
            return [t.strip() for t in re.split(r"[,\s]+", tags_val) if t.strip()]
        return []

    @classmethod
    def _assign_primary_tag(cls, tags: list[str]) -> str:
        """从多个标签中选择优先级最高的一个作为主标签。无匹配则归入日常运营风险。"""
        best_tag = cls.FALLBACK_TAG
        best_priority = 999
        for t in tags:
            p = cls.TAG_PRIORITY.get(t, 999)
            if p < best_priority:
                best_priority = p
                best_tag = t
        return best_tag

    @classmethod
    def _build_tag_groups(cls, df: pd.DataFrame) -> dict[str, pd.DataFrame]:
        """
        将 DataFrame 按主标签分组。
        每条情报只归入一个最高优先级的标签组。无标签 → 日常运营风险。
        返回 {tag_name: subset_df}，按优先级排序。
        """
        # 为每行计算主标签
        primary_tags = df.apply(lambda row: cls._assign_primary_tag(cls._extract_tags(row)), axis=1)
        df = df.copy()
        df["_primary_tag"] = primary_tags

        groups: dict[str, pd.DataFrame] = {}
        for tag in df["_primary_tag"].unique():
            groups[tag] = df[df["_primary_tag"] == tag]

        # 按优先级排序
        def sort_key(item: tuple[str, pd.DataFrame]) -> int:
            return cls.TAG_PRIORITY.get(item[0], 999)
        return dict(sorted(groups.items(), key=sort_key))

    # ── 动态标题 ──────────────────────────────────────

    def _get_report_title(self) -> str:
        if self.mode == "weekly":
            return "🏛️ ESG 全球地缘与合规周报 (Weekly Strategy Insight)"
        return "📊 ESG 全球供应链动态日报 (Daily Risk Radar)"

    # ── 生成入口 ──────────────────────────────────────

    def generate(self, path: str = "esg_global_report.md") -> None:
        if self.df.empty or "company" not in self.df.columns:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            title = self._get_report_title()
            Path(path).write_text(
                f"# {title}\n\n"
                f"> 📅 **生成时间**: {now}\n\n"
                f"> 今日未监测到相关有效情报。\n\n"
                f"> 🤖 *本报告由 ESG Intelligence Agent 驱动，经 DeepSeek 大模型进行实体消歧与智能摘要。*\n",
                encoding="utf-8",
            )
            logger.info(f"Empty report written to {path}")
            return

        lines = self._build_report()
        Path(path).write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"Report saved: {path} ({len(self.df)} intelligence items)")

    def _build_report(self) -> list[str]:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total = len(self.df)
        firms = self.df["company"].nunique() if "company" in self.df.columns else 0

        dates = pd.to_datetime(self.df.get("date", pd.Series(dtype=str)), errors="coerce")
        dmin = dates.min().strftime("%Y-%m-%d") if dates.notna().any() else "?"
        dmax = dates.max().strftime("%Y-%m-%d") if dates.notna().any() else "?"

        tag_groups = self._build_tag_groups(self.df)
        tag_count = len(tag_groups)

        title = self._get_report_title()
        lines: list[str] = [
            f"# {title}",
            "",
            f"> 📅 **生成时间**: {now}",
            f"> 📊 **情报总数**: {total} 条 | 企业: {firms} 家 | 风险标签: {tag_count} 类",
            f"> 📆 **覆盖时段**: {dmin} ~ {dmax}",
            "",
            "---",
            "",
            "## 📑 目录",
            "",
        ]

        for tag, sub_df in tag_groups.items():
            count = len(sub_df)
            lines.append(f"- **{tag}**（{count} 条）")
            for company in sorted(sub_df["company"].unique()):
                co_count = len(sub_df[sub_df["company"] == company])
                lines.append(f"  - {company}: {co_count} 条")
            lines.append("")

        lines += ["---", ""]

        for tag, sub_df in tag_groups.items():
            sub_df = sub_df.copy()
            sub_df["_sort_dt"] = pd.to_datetime(sub_df.get("date", pd.Series(dtype=str)), errors="coerce")
            sub_df = sub_df.sort_values("_sort_dt", ascending=False, na_position="last")

            lines.append(f"### {tag}")
            lines.append(f"> 共 {len(sub_df)} 篇情报")
            lines.append("")

            for _, row in sub_df.iterrows():
                lines.extend(self._render_item(row))

            lines += ["---", ""]

        lines += [
            "🤖 *本报告由 ESG Intelligence Agent 驱动，经 DeepSeek 大模型进行实体消歧与智能摘要。*",
            "⚠️  *数据来源为公开 RSS 新闻源，仅供决策参考，不构成投资或法律建议。*",
        ]
        return lines

    # URL 安全常量
    _SAFE_URL_FALLBACK = "https://news.google.com"

    @classmethod
    def _sanitize_url(cls, url: str) -> str:
        """校验 URL 安全性：不以 http 开头或长度异常 → 回退到 Google News 主页。"""
        if not url:
            return cls._SAFE_URL_FALLBACK
        url = url.strip()
        if not url.lower().startswith("http"):
            return cls._SAFE_URL_FALLBACK
        if len(url) < 12:
            return cls._SAFE_URL_FALLBACK
        return url

    @staticmethod
    def _render_item(row: pd.Series) -> list[str]:
        company = str(row.get("company", "")).strip()
        title   = str(row.get("title", row.get("title_cn", ""))).strip()
        url     = str(row.get("url", "")).strip()
        insight = str(row.get("insight", "")).strip()
        source  = str(row.get("source", "Unknown"))[:50].strip()
        date_s  = str(row.get("date", ""))[:10]

        # URL 安全过滤：防止非法字符串导致钉钉端无法点击
        url = MarkdownReportWriter._sanitize_url(url)

        parts: list[str] = []

        # Line 1: Company + Title (bold header — 钉钉适配：双换行分割)
        if company and title:
            parts.append(f"**{company}** | {title}\n\n")
        elif company:
            parts.append(f"**{company}**\n\n")
        elif title:
            parts.append(f"**{title}**\n\n")

        # Line 2: Executive Insight
        if insight:
            parts.append(f"💡 **高管洞察**：{insight}\n\n")

        # Line 3: Meta bar (date | source | link) in italic
        meta_segments = []
        if date_s:
            meta_segments.append(f"📅 {date_s}")
        if source:
            meta_segments.append(f"📰 {source}")
        if url:
            meta_segments.append(f"🔗 [阅读原文]({url})")
        if meta_segments:
            # 钉钉 italics: use *text* wrapping + double-spaces between segments
            meta_line = "   |   ".join(meta_segments)
            parts.append(f"*{meta_line}*\n\n")

        # Separator
        parts.append("---\n\n")
        return parts


# ─────────────────────────────────────────────────────────
# 智能体主控
# ─────────────────────────────────────────────────────────

class ESGIntelligenceAgent:
    """
    六阶段流水线（v9 — 双频动态播报 + 四重标签审计）：
      Phase 1   — 双频路由 RSS 多语种抓取
      Phase 2   — URL 级去重
      Phase 3   — 深度正文抓取
      Phase 2.5 — 实体出现校验
      Phase 4   — DeepSeek 大模型语义降噪 + 四重标签打标
      Phase 5   — Markdown 报告生成
    """

    DEEPSEEK_BASE_URL = "https://api.deepseek.com"
    DEEPSEEK_MODEL    = "deepseek-chat"

    # DeepSeek 定价 (USD per 1M tokens)
    _PRICE_INPUT_PER_1M  = 0.14   # $0.14 / 1M input tokens
    _PRICE_OUTPUT_PER_1M = 0.28   # $0.28 / 1M output tokens

    # Token 消耗追踪（实例级，每次 run() 重置）
    _token_stats: dict = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "total_cost_usd": 0.0,
    }

    @classmethod
    def _accumulate_tokens(cls, usage) -> None:
        """从 OpenAI response.usage 累积 token 统计。"""
        if usage is None:
            return
        try:
            prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion = int(getattr(usage, "completion_tokens", 0) or 0)
            total = int(getattr(usage, "total_tokens", 0) or 0) or (prompt + completion)
        except (TypeError, ValueError):
            return

        cls._token_stats["prompt_tokens"] += prompt
        cls._token_stats["completion_tokens"] += completion
        cls._token_stats["total_tokens"] += total
        cls._token_stats["total_cost_usd"] += (
            prompt * cls._PRICE_INPUT_PER_1M / 1_000_000
            + completion * cls._PRICE_OUTPUT_PER_1M / 1_000_000
        )

    @classmethod
    def _log_token_summary(cls) -> str:
        """输出 token 消耗日志，返回可用于报告底部的摘要字符串。"""
        s = cls._token_stats
        summary = (
            f"💰 Token 消耗: "
            f"输入 {s['prompt_tokens']:,} + 输出 {s['completion_tokens']:,} "
            f"= {s['total_tokens']:,} tokens | "
            f"预估费用 ${s['total_cost_usd']:.4f}"
        )
        logger.info(summary)
        return summary

    @classmethod
    def _reset_token_stats(cls) -> None:
        """每次 run() 开始时重置统计。"""
        cls._token_stats = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "total_cost_usd": 0.0,
        }

    @classmethod
    def _build_system_prompt(cls, company_names: list[str], mode: str) -> str:
        """构建 DeepSeek System Prompt - v11 华友钴业中心制。"""
        companies_str = "\n".join(f"  - {name}" for name in company_names)
        _ = mode  # 保留参数但不参与 prompt 构建（新 prompt 统一处理）
        return f"""# Role: ESG 供应链风险数据提取引擎

# Objective
你是一个结构化数据提取引擎。你的任务是读取一组繁杂的新闻流，将其清洗、去重、分类，并严格输出为 JSON 格式。

# Target Entities
重点监控对象及其关联企业：
{companies_str}

# Execution Logic (严格执行)
1. 事件聚类（最高优先级强制规则）：
   阅读所有新闻，将描述【同一核心事件】的多条新闻强制合并为一个独立事件。判断标准：
   - 同一企业在同一时间发生的同一类事件（如"特斯拉瑞典罢工""宝马韩国起火"）不论有多少家媒体以不同标题报道，都必须合并为一条 event。
   - 合并后的 event 必须聚合所有相关来源的媒体名称和对应 URL，放入 sources 数组。
   - 聚合后，必须产出一条最优的 core_event_title_en（标准英文摘要，用于去重）和一条精炼的 display_title_zh（中文标题，用于高管阅读）。
   - 严禁将同一事件的多个媒体报道拆分为多条独立 event。

2. 降噪判定 (is_valid_risk)：
   - 若实体错误（如"福特省长"、"福特医院"当成福特汽车），值为 false。
   - 若为正面或无实质风险事件（如减碳成功、引入机器人提升效率、正常商业投资），值为 false。
   - 若新闻涉及跨国供应链节点企业遭到属地政府、警方的正式合规调查（如环境污染调查、劳工纠纷调查、毒地/毒土处理调查、环保部门 Probe / Investigation），即使尚未引发物理停产或制裁，也必须判定为 true 并予以提取。
   - 只有明确包含物理停摆（关厂/减产）、劳资冲突（罢工/抗议）、质量与安全（召回/事故）、强力制裁等负面冲击时，值才为 true。
3. 严格分类 (risk_category) 与负面清单：
   - "早期合规预警"：属地政府/警方正式发起的环保调查、劳工合规审查、毒地/毒土处理 Probe、安全监察等尚未引发停产但已进入正式调查程序的早期合规阻力。此类别仅适用于调查/Probe/Investigation 性质的新闻，不适用于 NGO 指控或媒体质疑。
   - "供应链断裂预警"：仅限物理层面的供给中断（工厂因灾停产、矿端断供、核心供应商破产、物流瘫痪）。【负面清单】以下内容绝对不属于本类，必须归入 is_valid_risk=false 并直接丢弃：投资/股价波动、财报亏损、M&A/股权转让、需求疲软/销量下滑、新产品发布、技术合作、融资/增资。遇到这些话题时 risk_category 填入"无关噪音"且 is_valid_risk=false。
   - "市场准入预警"：仅限进出口禁令、关税惩罚、实体清单、强迫劳动货物扣留。【负面清单】绝对排除产品召回、质量事故、软件故障——这些归入"合规与运营危机"。
   - "合规与运营危机"：包含劳工罢工/抗议、重大安全事故（爆炸/矿难）、产品召回、车辆起火、软件安全缺陷、严重环保罚单。
   - "机构与声誉预警"：NGO指控、人权机构质询、评级下调等尚未演变为实质停产的高声誉风险事件。

4. 材料冲击判定 (is_direct_material_impact) — 绝对红线：
   你必须对每条 is_valid_risk=true 的事件额外输出布尔值字段 is_direct_material_impact，判定该事件是否对上游电池材料（前驱体/正极材料/镍钴锂资源）存在直接的供应链、订单或合规穿透冲击。
   
   【必须判定为 false 的硬性负面判例（Few-Shot）】：
   · 终端软件故障（OTA升级缺陷、车机黑屏/卡顿、自动驾驶软件算法召回）→ false。理由：纯软件层面，不涉及电池硬件更换。
   · 无人驾驶车祸（ADAS/FSD 导致的碰撞事故）→ false。理由：感知/控制算法缺陷，除非官方调查报告明确指出动力电池为起火主因。
   · 车机系统偶发爆炸/起火，且起火点被确认为 12V 低压电气系统或座舱电子设备（非高压动力电池）→ false。
   · 主机厂因软件问题发起 OTA 远程召回（无需进厂更换硬件）→ false。
   · 终端车企的营销争议、定价纠纷、经销商维权 → false。理由：不触及零部件采购体系。
   
   【必须判定为 true 的触发条件】：
   · 动力电池（高压电池包/电芯/模组）起火、召回、停产。
   · 正极材料/前驱体/电解液/隔膜等上游材料的质量缺陷被公开披露。
   · 镍/钴/锂矿端停产、禁运、出口管制、矿山事故。
   · 电池工厂（Gigafactory）爆炸、火灾、罢工导致停工。
   · 欧盟/美国针对电池材料的反倾销税、强迫劳动禁令、碳足迹准入门槛。
   
   【灰度条款 — 官方声明未出前默认 false】：
   · 若事件属于「电动汽车起火但起火原因未公布」，在无官方（NHTSA/车企/消防部门）明确声明指向动力电池前 → 默认 false。
   · 若事件属于「整车召回但未公布具体涉及零部件清单」→ 默认 false。
   
   【is_direct_material_impact 为 false 时 executive_insight 的强制写作规则】：
   当 is_direct_material_impact=false 时，executive_insight 必须如实写为：
   "该事件属于车企终端运营/技术故障，当前链条未传导至上游材料端。"
   严禁在 is_direct_material_impact=false 的情况下生搬硬套任何「订单波动」「供应链不确定性」「材料需求变化」等废话。

# Executive Insight 生成规则（严格执行 — 华友钴业中心制）
1. 身份锚定：华友钴业是全球领先的新能源锂电上游材料供应商，主营前驱体（Precursor）与正极材料（Cathode Active Material），核心下游客户包括特斯拉、宝马、奔驰、大众等全球主机厂及电池制造商。所有 insight 必须从华友钴业的产业位置出发进行传导推演。
2. 结构铁律：每条 insight 必须严格遵循【客观事实 + 华友钴业视角传导分析】两段式结构。绝对禁止出现任何形式的"建议""应当""需要""可考虑"等行为指导性措辞。
3. 传导分析强制切入点（至少覆盖以下一个维度）：
   - 订单冲击：下游主机厂客户的危机事件是否会影响其对华友前驱体/正极材料的采购订单量、交付节奏或定价条款。
   - 供应链连续性：上游矿端/中游制造环节的断供、停产、物流中断是否会影响华友的原料保障或产成品交付。
   - 海外项目准入合规：目标市场（欧盟、北美、东南亚）的监管政策变化是否会影响华友海外项目的环评审批、出口许可或供应链合规认证（如 CSRD/CSDDD/EU Battery Regulation）。
4. 字数红线：50-80 汉字或英文单词，低于 50 或超过 80 视为违规。
5. 禁止废话：严禁使用"可能影响运营""面临声誉风险""需持续关注""建议华友"等空洞套话。必须指明具体的传导环节和波及路径。
6. Tone Constraint (语气约束)：请始终以兼具严谨逻辑与务实精神的资深专业顾问身份撰写洞察。保持绝对的【客观、中立、克制】底色。严禁滥用耸动词汇（如'突发'、'震惊'、'致命'、'严重威胁'）。除非有确凿证据表明工厂在物理层面上【今天已经停产】，否则只做中性的事实陈述与逻辑推演。坚决避免'狼来了'效应。
7. 正例（合规）：
   - "宝马韩国市场因发动机起火被禁售，触发韩国《汽车管理法》召回程序。华友作为宝马电池材料上游供应商，需关注该车型所涉电池型号是否与华友正极材料供应体系存在关联，韩国市场禁售可能导致该车型减产，间接影响华友对韩系电池厂的正极材料出货排期。"
   - "特斯拉瑞典维修工人罢工规模虽缩减，但 IF Metall 工会仍维持封锁。北欧市场劳资冲突的持续发酵可能加速主机厂对供应链人权合规的审查力度，华友在印尼镍矿项目的劳工标准及出海合规文档将面临更严苛的欧盟 CSDDD 穿透审计。"
8. 反例（违规，绝不可输出）：
   - "可能影响运营" — 空洞无物，违反规则5。
   - "面临声誉风险" — 未说明传导链条，违反规则2。
   - "建议华友加强合规管理" — 包含行为建议，违反规则2（华友视角只推演传导，不给出建议）。
9. 【判例法 — 地缘政治因果传导铁律】绝对红线：
   · 如果美国国会/政府因"中国资本持股/中国供应链关联"而对欧洲车企（如梅赛德斯-奔驰、宝马等）发起审查或限制销售，其物理传导链条为："限制其在【美国本土】的销售，进而可能导致其全球电动车减产或迫使其供应链'去中国化'，从而间接冲击上游材料商（华友）的订单"。
   · 严禁直接推演为"影响该车企在华销量（在中国的销量）"。
   · 严禁因果倒置，必须严格区分【制裁发起国】、【受制裁主体市场】与【上游材料链】的真实传导方向。

# Output Format
你必须仅输出合法的 JSON 数据，不得包含任何 Markdown 标记或额外解释。JSON 结构必须如下：
{{
  "events": [
    {{
      "entity": "企业全称（必须精确匹配目标企业列表中某一项）",
      "core_event_title_en": "统一转换为标准英文的核心事件简短摘要（5-8个词）。注意：无论原始新闻是印尼语、德语还是中文，此字段必须强制翻译为英文，专门用于 Python 侧的 Jaccard 词级相似度去重。",
      "display_title_zh": "精炼、专业的纯中文新闻标题，供高管最终阅读。非中文/非英文新闻必须在此字段完成高质量汉化翻译。",
      "original_language": "识别原始新闻的语种，如 '印尼语', '英语', '德语', '中文'。",
      "executive_insight": "客观事实 + 华友钴业视角传导分析，50-80字",
      "date": "最新日期 YYYY-MM-DD",
      "sources": [{{"name": "媒体A", "url": "https://example.com/articleA"}}, {{"name": "媒体B", "url": "https://example.com/articleB"}}],
       "risk_category": "上述五大分类之一",
      "is_valid_risk": true,
      "is_direct_material_impact": true
    }},
    {{
      "entity": "亨利·福特医院",
      "core_event_title_en": "Henry Ford Hospital workers strike over working conditions",
      "display_title_zh": "亨利·福特医院发生罢工",
      "original_language": "英语",
      "executive_insight": "实体错误，非监控目标",
      "date": "2026-05-27",
      "sources": [{{"name": "Jacobin", "url": "https://jacobin.com/example"}}],
      "risk_category": "机构与声誉预警",
      "is_valid_risk": false,
      "is_direct_material_impact": false
    }}
  ]
}}

重要：每条 event 必须同时包含 core_event_title_en（英文）、display_title_zh（中文）和 original_language（语种）三个字段，缺一不可。核心去重字段为 core_event_title_en，无论原始语种如何都必须翻译为英文。
is_valid_risk 为 false 的条目也必须输出，以便审计追踪。所有新闻（包括被判定为无效的）都必须在 events 数组中占一条记录，通过 is_valid_risk 字段区分。每条记录都必须包含 is_direct_material_impact 布尔值字段，不可省略。
sources 字段中的每个元素必须包含 name（媒体名称）和 url（新闻原文直链，优先使用输入数据中提供的已解密 URL）。
如果没有收到任何新闻，请返回 {{ "events": [] }}。"""

    # 分批处理常量：每批最多发送的文章数
    BATCH_SIZE = 15

    @classmethod
    def _generate_ai_discovery_queries(cls, mode: str, company_names: list[str]) -> list[str]:
        """Phase 0.5: 让 AI 生成当日动态搜索词，填补静态关键词矩阵盲区。

        向 DeepSeek 发送专用 prompt，基于当前监控目标、风险类别和历史漏报教训，
        生成 5-10 条 Google News 搜索查询，捕获静态矩阵可能遗漏的新兴威胁。

        Returns:
            搜索查询字符串列表（未编码的原始查询）。
            失败时返回空列表，不阻断主流程。
        """
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            logger.info("[AI发现] DEEPSEEK_API_KEY 未设置，跳过动态查询生成")
            return []

        companies_str = "\n".join(f"  - {name}" for name in company_names)
        mode_label = "daily（劳工权益、环境污染、社区冲突）" if mode == "daily" else "weekly（覆盖全部 6 类风险主题：劳工、环境、社区、欧盟准入、美国地缘封锁、资源国民族主义）"

        system_prompt = f"""你是一个 ESG 供应链风险情报分析师。你负责为一套自动化监控系统生成每日补充搜索词。

## 当前监控矩阵
**已监控企业**（{len(company_names)} 家）：
{companies_str}

**当前模式**: {mode_label}

**已知盲区教训**:
- 2026年6月16日漏报「紫金矿业遭美国 CBP 扣押令(WRO)」事件，因为紫金矿业不在监控名单且 WRO/forced labor 关键词未覆盖。

## 你的任务
基于当前日期、已监控企业、风险类别和历史盲区教训，生成 5-10 条 Google News 搜索查询（纯英文），这些查询应该能捕获我们静态关键词矩阵可能遗漏的新兴威胁。

### 查询设计原则
1. **不重复已有覆盖**: 如果某公司已在监控矩阵中，不要生成针对该公司的查询
2. **横向扩展**: 关注同行业/同产业链的未监控企业（如其他中国矿企、电池材料商）
3. **纵向追溯**: 关注上游原料产地（非洲铜钴带、南美锂三角、印尼镍矿）的新兴事件
4. **监管动向**: 关注美国海关(CBP)、欧盟、资源国政府的最新制裁/立法/禁令
5. **产业趋势**: 关注可能引发供应链重构的重大技术、贸易或地缘变化

### 输出格式
返回纯 JSON 对象，格式：{{"queries": ["query string 1", "query string 2", ...]}}
每条 query 是可直接用于 Google News 搜索的布尔表达式。
如果没有适合补充的查询，返回 {{"queries": []}}。
仅输出 JSON，不要包含任何解释或 Markdown 标记。"""

        today_str = datetime.now().strftime("%Y-%m-%d")
        user_message = f"今天是 {today_str}。请基于当前监控矩阵和盲区教训，生成今日补充搜索查询。"

        try:
            client = OpenAI(api_key=api_key, base_url=cls.DEEPSEEK_BASE_URL)
            response = client.chat.completions.create(
                model=cls.DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                response_format={"type": "json_object"},
                temperature=0.3,
                max_tokens=1024,
            )
            cls._accumulate_tokens(response.usage)
            raw = response.choices[0].message.content or ""

            # 解析 JSON
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # 尝试从 markdown 代码块中提取
                import re as _re
                m = _re.search(r'\{.*\}', raw, _re.DOTALL)
                if m:
                    data = json.loads(m.group())
                else:
                    logger.warning("[AI发现] 无法解析查询生成响应 JSON")
                    return []

            queries = data.get("queries", [])
            if not isinstance(queries, list):
                return []

            valid_queries = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
            logger.info(
                f"[AI发现] 生成了 {len(valid_queries)} 条动态搜索查询: "
                f"{valid_queries[:3]}{'...' if len(valid_queries) > 3 else ''}"
            )
            return valid_queries

        except Exception as e:
            logger.warning(f"[AI发现] 查询生成失败 ({type(e).__name__}: {e})，回退到静态矩阵")
            return []

    @classmethod
    def _weekly_threat_landscape_review(
        cls, events: list[dict], report_path: str, token_summary: str
    ) -> None:
        """Phase 6 (weekly only): LLM 分析本周监控盲区，追加到周报末尾。

        将本周所有捕获事件发送给 DeepSeek，分析：
        1. 哪些实体/关键词/区域在当前监控矩阵中缺失
        2. 哪些新兴威胁模式未被覆盖
        3. 对 esg_sources.yaml 和 config.yaml 的具体补充建议

        输出追加到周报文件末尾作为「🔍 监控矩阵盲区分析」章节。
        """
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            logger.info("[周度审查] DEEPSEEK_API_KEY 未设置，跳过威胁态势审查")
            return

        if not events:
            placeholder = "\n\n---\n\n## 🔍 监控矩阵盲区分析\n\n本周未捕获风险事件，无法进行态势审查。\n"
            with open(report_path, "a", encoding="utf-8") as f:
                f.write(placeholder)
            logger.info("[周度审查] 无事件数据，写入空审查占位")
            return

        # 构建事件摘要
        events_summary_lines = []
        for i, e in enumerate(events[:50]):  # 最多送 50 条防止 token 爆炸
            entity = e.get("entity", "未知")
            title = e.get("core_event_title_en") or e.get("display_title_zh", "")
            cat = e.get("risk_category", "")
            valid = e.get("is_valid_risk", False)
            material = e.get("is_direct_material_impact", False)
            events_summary_lines.append(
                f"{i+1}. [{entity}] {title} | 类别:{cat} | 有效:{valid} | 实质:{material}"
            )
        events_text = "\n".join(events_summary_lines)

        system_prompt = """你是一个 ESG 监控系统架构师。你会收到本周系统捕获的所有风险事件摘要。
请分析当前监控矩阵的盲区，输出以下内容：

1. **缺失实体**: 本周事件中出现了哪些未被纳入监控的重要企业/组织？
2. **缺失关键词**: 哪些风险类型的关键词组合未被覆盖，导致可能漏报？
3. **新兴威胁模式**: 本周事件中是否出现了新的制裁/立法/产业趋势，需要新增监控轨道？
4. **具体建议**: 对 esg_sources.yaml 新增轨道的具体 YAML 配置建议。

用中文输出，简洁专业。如果当前矩阵覆盖良好，直接说明"本周未发现明显盲区"。
输出直接追加到周报文件，格式为 Markdown。"""

        user_message = f"以下是本周捕获的风险事件（共 {len(events)} 条，展示前 {min(len(events), 50)} 条）:\n\n{events_text}"

        try:
            client = OpenAI(api_key=api_key, base_url=cls.DEEPSEEK_BASE_URL)
            response = client.chat.completions.create(
                model=cls.DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.2,
                max_tokens=2048,
            )
            cls._accumulate_tokens(response.usage)
            analysis = response.choices[0].message.content or ""

            # 追加到周报文件
            section = f"\n\n---\n\n## 🔍 监控矩阵盲区分析\n\n{analysis}\n"
            with open(report_path, "a", encoding="utf-8") as f:
                f.write(section)
            logger.info(f"[周度审查] 盲区分析已追加到 {report_path}")

        except Exception as e:
            logger.warning(f"[周度审查] LLM 调用失败 ({type(e).__name__}: {e})，跳过态势审查")

    @classmethod
    def _extract_events_object(cls, text: str) -> Optional[list]:
        """从 LLM 回复中提取 v10 events 格式的 JSON 并校验。

        v10 格式：{ "events": [...] }
        1. 正则提取最外层 JSON 对象，兼容 markdown 代码块包裹。
        2. json.loads 解析。
        3. 提取 "events" 键，校验为非空 list。

        Returns:
            解析成功返回 events list[dict]，失败返回 None。
        """
        if not text or not text.strip():
            return None
        # 步骤1: 去除可能的 markdown 代码块包裹
        clean = text.strip()
        code_block_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", clean, re.DOTALL)
        if code_block_match:
            clean = code_block_match.group(1).strip()
        # 步骤2: 正则提取最外层 JSON 对象
        match = re.search(r"\{.*\}", clean, re.DOTALL)
        if not match:
            logger.warning("_extract_events_object: no JSON object found in LLM response.")
            return None
        candidate = match.group(0)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as e:
            logger.warning(f"_extract_events_object: JSON parse failed: {e}")
            return None
        # 步骤3: 结构校验
        if not isinstance(parsed, dict):
            logger.warning("_extract_events_object: parsed result is not a dict/object.")
            return None
        events = parsed.get("events", None)
        if not isinstance(events, list):
            logger.warning("_extract_events_object: 'events' field missing or not a list.")
            return None
        if len(events) == 0:
            return []  # 合法空回复
        # 步骤4: 至少包含一条有效企业名
        has_entity = any(
            isinstance(item, dict) and str(item.get("entity", "")).strip()
            for item in events
        )
        if not has_entity:
            logger.warning("_extract_events_object: events array has no items with non-empty 'entity' field — discarding.")
            return None
        return events

    # ── 语义合并常量 ──────────────────────────────────────
    _MERGE_SIMILARITY_THRESHOLD = 0.45  # Jaccard 相似度阈值：>= 此值视为同一事件
    _WORD_PATTERN = re.compile(r"\w+", re.UNICODE)

    @classmethod
    def _merge_same_company_events(cls, events: list[dict]) -> list[dict]:
        """同公司同质化事件语义合并（v12 — 跨语种英文去重）。

        在 LLM 跨批次处理时，同一企业的同一事件可能被分散到不同批次中，
        LLM 无法跨批次合并。此函数在 Python 端执行二次合并：
        1. 按 entity（企业名）分组。
        2. 每组内对 core_event_title_en（LLM 已规范为英文）做 Jaccard 词级相似度比对。
           - 由于所有语种的标题已统一翻译为英文，0.45 阈值实现跨语种精准聚合。
        3. 相似度 >= 阈值的合并为一条，聚合所有 sources（带语种标签）并去重。
        4. 保留最新的 date 和最优的 display_title_zh 作为合并后字段。
        """
        if not events:
            return []

        # 按企业分组
        entity_groups: dict[str, list[dict]] = {}
        for e in events:
            ent = str(e.get("entity", "")).strip().lower()
            if ent not in entity_groups:
                entity_groups[ent] = []
            entity_groups[ent].append(e)

        merged: list[dict] = []
        for ent, group in entity_groups.items():
            if len(group) <= 1:
                merged.extend(group)
                continue

            used = [False] * len(group)
            for i in range(len(group)):
                if used[i]:
                    continue
                base = group[i]
                # v12: 使用 core_event_title_en 做跨语种去重
                base_title = str(base.get("core_event_title_en", base.get("core_event_title", ""))).strip().lower()
                base_tokens = set(cls._WORD_PATTERN.findall(base_title))

                all_sources: list[dict] = []
                seen_source_urls: set[str] = set()
                # 收集 base 的 sources
                for s in base.get("sources", []):
                    if isinstance(s, dict):
                        s_url = str(s.get("url", "")).strip()
                        if s_url and s_url not in seen_source_urls:
                            seen_source_urls.add(s_url)
                            all_sources.append(s)
                        elif not s_url:
                            all_sources.append(s)
                    elif isinstance(s, str):
                        all_sources.append({"name": s, "url": ""})

                all_dates = [str(base.get("date", ""))[:10]]

                for j in range(i + 1, len(group)):
                    if used[j]:
                        continue
                    other = group[j]
                    # v12: 使用 core_event_title_en 做跨语种去重
                    other_title = str(other.get("core_event_title_en", other.get("core_event_title", ""))).strip().lower()
                    other_tokens = set(cls._WORD_PATTERN.findall(other_title))

                    if not base_tokens or not other_tokens:
                        continue

                    overlap = len(base_tokens & other_tokens)
                    union = len(base_tokens | other_tokens)
                    similarity = overlap / union if union > 0 else 0

                    if similarity >= cls._MERGE_SIMILARITY_THRESHOLD:
                        used[j] = True
                        # 合并 sources
                        for s in other.get("sources", []):
                            if isinstance(s, dict):
                                s_url = str(s.get("url", "")).strip()
                                if s_url and s_url not in seen_source_urls:
                                    seen_source_urls.add(s_url)
                                    all_sources.append(s)
                                elif not s_url:
                                    all_sources.append(s)
                            elif isinstance(s, str):
                                all_sources.append({"name": s, "url": ""})
                        all_dates.append(str(other.get("date", ""))[:10])
                        logger.info(
                            f"[语义合并] {ent}: "
                            f"'{base_title[:50]}…' ← '{other_title[:50]}…' "
                            f"(相似度={similarity:.2f})"
                        )

                # v12: 保留双标题结构，优先使用 display_title_zh
                display_zh = str(
                    base.get("display_title_zh")
                    or base.get("core_event_title_en")
                    or base.get("core_event_title", "")
                ).strip()
                merged_event: dict = {
                    "entity": base.get("entity", ""),
                    "core_event_title_en": base.get("core_event_title_en", base.get("core_event_title", "")),
                    "display_title_zh": display_zh,
                    "original_language": base.get("original_language", ""),
                    "executive_insight": base.get("executive_insight", ""),
                    "date": max(d for d in all_dates if d) if all_dates else base.get("date", ""),
                    "sources": all_sources,
                    "risk_category": base.get("risk_category", ""),
                    "is_valid_risk": base.get("is_valid_risk", True),
                    "is_direct_material_impact": base.get("is_direct_material_impact", True),
                }
                merged.append(merged_event)

        logger.info(
            f"语义合并: {len(events)} raw events -> {len(merged)} after same-company semantic dedup "
            f"(threshold={cls._MERGE_SIMILARITY_THRESHOLD})"
        )
        return merged

    @classmethod
    def _generate_v10_report_and_filter(cls, all_events: list[dict], mode: str, report_path: str = "esg_global_report.md") -> list[dict]:
        """Python 确定性渲染流水线：过滤 → 语义合并 → 分组 → 生成 Markdown 报告。

        所有降噪逻辑由 Python if-else 物理执行，不依赖 LLM 判断。
        Returns: v9 兼容的 dict 列表，用于下游日志统计。
        """
        # ── 1. 确定性降噪（Python 物理隔绝） ──
        invalid_events: list[dict] = []
        non_material_events: list[dict] = []
        valid_events: list[dict] = []
        for event in all_events:
            if not isinstance(event, dict):
                continue
            if event.get("is_valid_risk") is False:
                invalid_events.append(event)
            elif event.get("is_direct_material_impact") is False:
                non_material_events.append(event)
            else:
                valid_events.append(event)

        # 审计日志（v12：优先使用 core_event_title_en）
        for e in invalid_events:
            title_key = e.get("core_event_title_en") or e.get("core_event_title", "?")
            logger.info(f"[v12 降噪] 已过滤(无效风险): {e.get('entity', '?')} | {str(title_key)[:60]}")
        for e in non_material_events:
            title_key = e.get("core_event_title_en") or e.get("core_event_title", "?")
            logger.info(f"[v12 降噪] 已过滤(非材料冲击): {e.get('entity', '?')} | {str(title_key)[:60]}")
        logger.info(
            f"Python 降噪: {len(invalid_events)} invalid + {len(non_material_events)} non-material -> dropped, "
            f"{len(valid_events)} material-impact -> report"
        )

        # ── 1.5. 同公司同质化事件语义合并 ──
        pre_merge_count = len(valid_events)
        valid_events = cls._merge_same_company_events(valid_events)
        if len(valid_events) < pre_merge_count:
            logger.info(
                f"语义合并: {pre_merge_count} -> {len(valid_events)} events "
                f"(移除 {pre_merge_count - len(valid_events)} 条同质化重复)"
            )

        # ── 1.6. 终极 LLM 全局聚合层 (Final Convergence) ──
        # 解决跨批次 Semantic Drift 导致的假性重复 — 当前 valid_events 通常已 ≤10 条
        if len(valid_events) > 1:
            pre_converge = len(valid_events)
            valid_events = cls._llm_global_convergence(valid_events)
            if len(valid_events) < pre_converge:
                logger.info(
                    f"LLM 全局聚合: {pre_converge} -> {len(valid_events)} events "
                    f"(合并 {pre_converge - len(valid_events)} 条跨批次重复)"
                )

        # ── 1.65. Google News URL 延迟解包（Lazy Unwrapping） ──
        # 全局聚合完成后，遍历所有幸存事件的 sources 数组，
        # 将 Google News 加密链接（news.google.com/rss/articles）还原为真实源站直链。
        unwrap_count = 0
        for event in valid_events:
            sources = event.get("sources")
            if not isinstance(sources, list):
                continue
            for s in sources:
                if isinstance(s, dict):
                    src_url = str(s.get("url", "")).strip()
                    if src_url and "news.google.com/rss/articles" in src_url:
                        resolved = cls._unwrap_google_news_url(src_url)
                        if resolved != src_url:
                            s["url"] = resolved
                            unwrap_count += 1
        if unwrap_count:
            logger.info(f"Google News URL 延迟解包: {unwrap_count} 个加密链接已还原为真实源站直链")

        # ── 1.7. 静默阻断：今日无风险事件 ──
        if not valid_events:
            logger.info(
                f"今日分析样本 {len(all_events)} 篇，命中合规红线 0 篇，执行静默阻断。"
            )
            # 写入占位报告，覆盖旧日期残留报告，向使用者明确系统今日已成功巡检
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if mode == "weekly":
                title = "🏛️ ESG 全球地缘与合规周报 (Weekly Strategy Insight)"
                first_line = "🔮【宏观合规战略】全球地缘与准入壁垒周报"
            else:
                title = "🏛️ ESG 全球供应链动态日报 (Daily Intelligence)"
                first_line = "全球供应链合规与风险速递"
            placeholder = "\n".join([
                f"# {title}",
                "",
                f"> {first_line}",
                f"> 📅 **生成时间**: {now_str}",
                f"> 📊 **情报总数**: 0 条 | 涉及企业: 0 家",
                "",
                "---",
                "",
                "## 📑 今日无风险事件",
                "",
                "今日无新增实质性供应链断裂与合规风险。",
                f"系统今日已成功巡检，分析样本 {len(all_events)} 篇，均未命中合规红线。",
                "",
                "---",
                "",
                "🤖 *本报告由 ESG Intelligence Agent 自动生成，数据来源于公开新闻源。*",
                "⚠️  *仅供决策参考，不构成投资或法律建议。*",
            ])
            Path(report_path).write_text(placeholder, encoding="utf-8")
            logger.info(f"静默阻断占位报告已写入: {report_path}")
            return []

        # ── 2. 按风险类别分组 ──
        categorized: dict[str, list[dict]] = {
            "早期合规预警": [],
            "供应链断裂预警": [],
            "政策与市场准入": [],
            "合规与运营危机": [],
            "机构与声誉预警": [],
        }
        for event in valid_events:
            cat = str(event.get("risk_category", "")).strip()
            # 映射 LLM 可能输出的 "市场准入预警" -> "政策与市场准入"
            if cat == "市场准入预警":
                cat = "政策与市场准入"
            if cat in categorized:
                categorized[cat].append(event)
            else:
                categorized["合规与运营危机"].append(event)

        # ── 3. 推断时间跨度 ──
        all_dates = [e.get("date", "") for e in valid_events]
        if all_dates:
            dmin = min(all_dates)
            dmax = max(all_dates)
        else:
            dmin = dmax = "?"

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 严格按 mode 参数决定标题 — daily/weekly 绝对互斥，禁止自动推断
        if mode == "weekly":
            title = "🏛️ ESG 全球地缘与合规周报 (Weekly Strategy Insight)"
            first_line = "🔮【宏观合规战略】全球地缘与准入壁垒周报"
        else:
            title = "🏛️ ESG 全球供应链动态日报 (Daily Intelligence)"
            first_line = "全球供应链合规与风险速递"

        # ── 4. 生成 Markdown 报告 ──
        lines: list[str] = [
            f"# {title}",
            "",
            f"> {first_line}",
            f"> 📅 **生成时间**: {now_str}",
            f"> 📊 **情报总数**: {len(valid_events)} 条 | 涉及企业: {len(set(e.get('entity', '') for e in valid_events))} 家",
            f"> 📆 **覆盖时段**: {dmin} ~ {dmax}",
            "",
            "---",
            "",
            "## 📑 目录",
            "",
        ]

        category_cn_names = {
            "早期合规预警": "早期合规预警",
            "供应链断裂预警": "供应链断裂预警",
            "政策与市场准入": "政策与市场准入",
            "合规与运营危机": "合规与运营危机",
            "机构与声誉预警": "机构与声誉预警",
        }
        category_descriptions = {
            "早期合规预警": "尚未引发断供或停产，但面临官方环保调查、劳工审查或严重违规指控的早期阻力",
            "供应链断裂预警": "仅限物理层面的供给中断（工厂停产、物流瘫痪、核心供应商断供）",
            "政策与市场准入": "仅限引发无法卖货/买货的事件（实体清单、关税惩罚、进出口禁令、强迫劳动扣留）",
            "合规与运营危机": "劳工罢工、重大安全事故、产品召回、严重环保罚单引发的即期运营阻断",
            "机构与声誉预警": "NGO指控、人权机构质询、评级下调等尚未演变为实质停产的高声誉风险事件",
        }

        # 目录 — 单行简洁格式，无子项拆分
        for cat_key, cat_name in category_cn_names.items():
            evs = categorized.get(cat_key, [])
            if not evs:
                continue
            lines.append(f"- **【{cat_name}】**（{len(evs)} 条）")
        lines.append("")

        lines += ["---", ""]

        # 各分类内容
        for cat_key, cat_name in category_cn_names.items():
            evs = categorized.get(cat_key, [])
            if not evs:
                continue
            desc = category_descriptions.get(cat_key, "")
            lines.append(f"## 【{cat_name}】")
            lines.append(f"> {desc}")
            lines.append("")

            # 按日期降序排列
            evs.sort(key=lambda e: str(e.get("date", "")), reverse=True)

            for e in evs:
                entity = str(e.get("entity", "")).strip()
                # v12: 使用汉化后的 display_title_zh 作为主标题
                title_text = str(
                    e.get("display_title_zh")
                    or e.get("core_event_title_en")
                    or e.get("core_event_title", "")
                ).strip()
                insight = str(e.get("executive_insight", "")).strip()
                date = str(e.get("date", ""))[:10]
                # v12: 信息源聚合，格式 [媒体名 (语种)](解密URL)
                sources_raw = e.get("sources", [])
                event_lang = str(e.get("original_language", "")).strip()
                source_links: list[str] = []
                seen_source_labels: set[str] = set()
                if isinstance(sources_raw, list):
                    for s in sources_raw:
                        if isinstance(s, dict):
                            name = str(s.get("name", "")).strip()
                            src_url = str(s.get("url", "")).strip()
                            # 优先使用 source 级别语种，回退到 event 级别
                            s_lang = str(s.get("original_language", "")) or event_lang
                            if name:
                                # 二次解密：LLM 可能回传未解密的 Google News URL
                                if src_url and "news.google.com" in src_url:
                                    src_url = resolve_news_url(src_url)
                                # v12: 拼接语种标签，格式 [媒体名 (语种)]
                                label = f"{name} ({s_lang})" if s_lang else name
                                if label in seen_source_labels:
                                    continue
                                seen_source_labels.add(label)
                                if src_url and src_url.lower().startswith("http"):
                                    source_links.append(f"[{label}]({src_url})")
                                else:
                                    source_links.append(label)
                        elif isinstance(s, str) and s.strip():
                            if s.strip() not in seen_source_labels:
                                seen_source_labels.add(s.strip())
                                source_links.append(s.strip())
                if not source_links:
                    source_links.append("Unknown")
                sources_str = ", ".join(source_links)

                lines.append(f"**{entity} | {title_text}**")
                lines.append("")
                lines.append(f"💡 高管洞察：{insight}")
                lines.append("")
                lines.append(f"📅 {date} | 📰 信息源聚合：{sources_str}")
                lines.append("")
                lines.append("---")
                lines.append("")

        lines += [
            "---",
            "",
            "🤖 *本报告由 ESG Intelligence Agent 自动生成，数据来源于公开新闻源。*",
            "⚠️  *仅供决策参考，不构成投资或法律建议。*",
        ]

        report_md = "\n".join(lines)
        Path(report_path).write_text(report_md, encoding="utf-8")
        logger.info(f"v10 report written: {report_path} ({len(valid_events)} valid / {len(invalid_events)} invalid)")

        # 返回 v12 兼容格式用于 run() 日志统计
        compatible: list[dict] = []
        for e in valid_events:
            sources_raw = e.get("sources", [])
            if isinstance(sources_raw, list):
                source_names = []
                for s in sources_raw:
                    if isinstance(s, dict):
                        s_name = str(s.get("name", ""))
                        s_lang = str(s.get("original_language", "")) or str(e.get("original_language", ""))
                        label = f"{s_name} ({s_lang})" if s_name and s_lang else s_name
                        if label:
                            source_names.append(label)
                    elif isinstance(s, str):
                        source_names.append(s)
                source_str = ", ".join(n for n in source_names if n)
            else:
                source_str = str(sources_raw) if sources_raw else "Unknown"
            risk_cat = str(e.get("risk_category", "")).strip()
            # v12: title 使用 display_title_zh
            title_val = str(
                e.get("display_title_zh")
                or e.get("core_event_title_en")
                or e.get("core_event_title", "")
            ).strip()
            compatible.append({
                "company": str(e.get("entity", "")).strip(),
                "title": title_val,
                "insight": str(e.get("executive_insight", "")).strip(),
                "source": source_str,
                "date": str(e.get("date", ""))[:10],
                "tags": [risk_cat] if risk_cat else ["日常运营风险"],
                "url": str(e.get("url", "")).strip(),
            })
        return compatible

    @classmethod
    def _build_articles_text(cls, batch: list[dict], start_idx: int) -> str:
        """将一批文章组装为 LLM user message 文本。"""
        parts = []
        for i, item in enumerate(batch):
            display_name = item.get("display_name", item.get("company_name_zh", ""))
            parts.append(
                f"--- 文章 #{start_idx + i + 1} ---\n"
                f"原始标题: {item.get('title', '')}\n"
                f"语种: {item.get('lang', 'en-US')}\n"
                f"日期: {fmt_date(item.get('parsed_date') or item.get('date', ''))}\n"
                f"来源: {item.get('source', 'Unknown')}\n"
                f"搜索目标企业: {display_name}\n"
                f"来源轨道: {item.get('track_label', '')}\n"
                f"主题类别: {item.get('topic_category', '')}\n"
                f"正文摘要: {item.get('raw_summary', '')[:300]}\n"
                f"URL: {item.get('url', '')}"
            )
        return "\n\n".join(parts)

    # ── Google News URL 延迟解包 ──────────────────────────

    _GOOGLE_RSS_ARTICLE_RE = re.compile(r"news\.google\.com/rss/articles")

    @classmethod
    def _unwrap_google_news_url(cls, url: str) -> str:
        """将 Google News RSS 加密链接还原为底层真实媒体直链。

        仅处理包含 'news.google.com/rss/articles' 的 URL；
        使用 requests.head 跟随重定向链，返回最终跳转地址。
        解析失败或非 Google News 链接时原样返回。
        """
        if not url or not cls._GOOGLE_RSS_ARTICLE_RE.search(url):
            return url
        try:
            resp = requests.head(
                url,
                headers=FETCH_HEADERS,
                allow_redirects=True,
                timeout=5,
            )
            final_url = resp.url
            if (
                final_url != url
                and "google.com" not in final_url
                and final_url.lower().startswith("http")
            ):
                logger.debug(f"Google News unwrapped: {url[:60]}... -> {final_url[:80]}...")
                return final_url
            else:
                logger.debug(f"Google News unwrap: no redirect for {url[:60]}...")
        except Exception as exc:
            logger.debug(f"Google News unwrap failed [{url[:60]}...]: {exc}")
        return url

    @classmethod
    def _unwrap_event_sources(cls, event: dict) -> dict:
        """对单个 event 的所有 source URL 执行 Google News 延迟解包。

        深度遍历 sources 数组中每个元素的 url 字段，
        将 Google News 加密链接替换为真实源站直链。
        """
        sources = event.get("sources")
        if not isinstance(sources, list):
            return event
        for s in sources:
            if isinstance(s, dict):
                src_url = str(s.get("url", "")).strip()
                if src_url and "news.google.com" in src_url:
                    s["url"] = cls._unwrap_google_news_url(src_url)
        return event

    @classmethod
    def _llm_global_convergence(cls, valid_events: list[dict]) -> list[dict]:
        """终极 LLM 全局聚合层 — 解决跨批次 Semantic Drift 导致的假性重复。

        当 Python Jaccard 去重后仍有 ≤10 条 valid_events 时，
        将全部事件一次性喂给 LLM，由其执行语义级别的全局去重合并。
        输出保持与 valid_events 相同的结构。
        调用失败时安全回退，返回原始列表。
        """
        if not valid_events or len(valid_events) <= 1:
            return valid_events

        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            logger.warning("[LLM全局聚合] DEEPSEEK_API_KEY 未设置，跳过全局聚合")
            return valid_events

        # 构建输入 JSON
        events_json = json.dumps(valid_events, ensure_ascii=False, indent=2)

        system_msg = """你是一个事件去重引擎。你会收到一组已通过技术降噪的 valid_events。
你的任务是：将描述同一核心商业/地缘事件的条目彻底合并为一条。
合并规则：
1. 合并时提炼出一个最精准、专业的 display_title_zh（中文标题）。
2. 将所有相关媒体名称和 URL 合并到 sources 数组中，保留 original_language 标签，去重。
3. 保留最新的 date、最完整的 executive_insight。
4. 输出格式必须与输入完全一致（数组），不得添加任何额外字段。
5. 如果输入中没有重复事件，直接返回原数组。
仅输出合法的 JSON 数组，不要包含任何解释或 Markdown 标记。"""

        try:
            client = OpenAI(api_key=api_key, base_url=cls.DEEPSEEK_BASE_URL)
            response = client.chat.completions.create(
                model=cls.DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": f"以下是 valid_events JSON 数组:\n{events_json}"},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=4096,
            )
            raw = response.choices[0].message.content or ""
            cls._accumulate_tokens(response.usage)
            # 提取 JSON 数组
            match = re.search(r"\[.*\]", raw, re.DOTALL)
            if not match:
                logger.warning("[LLM全局聚合] 响应中未找到 JSON 数组，回退到原始列表")
                return valid_events
            merged = json.loads(match.group(0))
            if isinstance(merged, list) and len(merged) > 0:
                logger.info(f"[LLM全局聚合] 成功: {len(valid_events)} -> {len(merged)}")
                return merged
            else:
                logger.warning("[LLM全局聚合] 返回空数组或非数组结构，回退到原始列表")
                return valid_events
        except Exception as exc:
            logger.warning(f"[LLM全局聚合] 调用失败 ({type(exc).__name__}: {exc})，回退到原始列表")
            return valid_events

    def process_intelligence_with_llm(self, raw_data_list: list[dict], mode: str = "daily") -> list[dict]:
        """分批将文章发送至 DeepSeek，收集所有 v10 原始 events 数据。

        ═══ 核心改动（v10 流水线） ═══
        · BATCH_SIZE=15 分批处理，防止单次数据量过大导致 JSON 崩溃。
        · 使用 _extract_events_object() 提取 { "events": [...] }。
        · 所有批次的事件汇总后，由 _generate_v10_report_and_filter() 统一过滤和渲染。
        · 批次失败直接丢弃该批次数据，绝不回退到原始数据。

        Returns:
            所有批次汇总的 v10 原始 events 列表（含 is_valid_risk=true/false），
            供 _generate_v10_report_and_filter 进行 Python 确定性降噪。
        """
        if not raw_data_list:
            logger.info("No raw data to process with LLM.")
            return []

        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            logger.error("环境变量 DEEPSEEK_API_KEY 未设置，无法调用 LLM，返回空结果。")
            return []

        company_names = self.config.get_all_company_display_names()
        total = len(raw_data_list)
        batch_count = (total + self.BATCH_SIZE - 1) // self.BATCH_SIZE
        logger.info(
            f"LLM processing: {total} articles -> {batch_count} batch(es) "
            f"(BATCH_SIZE={self.BATCH_SIZE}, mode={mode})"
        )

        all_events: list[dict] = []
        client = OpenAI(api_key=api_key, base_url=self.DEEPSEEK_BASE_URL)

        for batch_idx in range(batch_count):
            start = batch_idx * self.BATCH_SIZE
            end = min(start + self.BATCH_SIZE, total)
            batch = raw_data_list[start:end]
            logger.info(f"  Batch {batch_idx + 1}/{batch_count}: articles #{start + 1}-{end} ({len(batch)} items)")

            user_message = self._build_articles_text(batch, start)

            try:
                response = client.chat.completions.create(
                    model=self.DEEPSEEK_MODEL,
                    messages=[
                        {"role": "system", "content": self._build_system_prompt(company_names, mode)},
                        {"role": "user", "content": user_message},
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.1,
                    max_tokens=8192,
                )

                raw_output = response.choices[0].message.content or ""
                self._accumulate_tokens(response.usage)
                logger.info(f"  LLM response received ({len(raw_output)} chars).")

                parsed = self._extract_events_object(raw_output)
                if parsed is None:
                    logger.warning(
                        f"  ⚠ Batch {batch_idx + 1} failed JSON extraction/validation — "
                        f"DISCARDING {len(batch)} articles to prevent noise pollution."
                    )
                    continue

                all_events.extend(parsed)
                valid_in_batch = sum(1 for e in parsed if e.get("is_valid_risk") is not False)
                logger.info(f"  Batch {batch_idx + 1}: {len(parsed)} events ({valid_in_batch} valid) extracted.")

            except Exception as e:
                logger.warning(
                    f"  ⚠ Batch {batch_idx + 1} LLM call failed ({type(e).__name__}: {e}) — "
                    f"DISCARDING {len(batch)} articles to prevent noise pollution."
                )
                continue

        logger.info(f"LLM processing complete: {len(all_events)} total events from {batch_count} batch(es).")
        return all_events

    # ── 系统级报警（钉钉 Webhook） ──────────────────────────

    @staticmethod
    def _send_system_alert(message: str) -> None:
        """向预设钉钉 Webhook 发送 FATAL 级系统报警。

        判空保护：若 DINGTALK_WEBHOOK_URL 为空，仅在控制台打印 ERROR 日志。
        网络异常时静默失败，不影响主流程。
        """
        logger.error(f"[SYSTEM_ALERT] {message}")
        if not DINGTALK_WEBHOOK_URL:
            return
        try:
            payload = {
                "msgtype": "text",
                "text": {"content": message},
            }
            resp = requests.post(
                DINGTALK_WEBHOOK_URL,
                headers={"Content-Type": "application/json"},
                data=json.dumps(payload),
                timeout=10,
            )
            logger.info(f"[SYSTEM_ALERT] 钉钉报警已发送，响应: {resp.status_code}")
        except Exception as exc:
            logger.error(f"[SYSTEM_ALERT] 钉钉报警发送失败: {exc}")

    # ── 钉钉推送 ──────────────────────────────────────────

    def push_to_dingtalk(self, report_path: str = "esg_global_report.md", mode: str = "daily") -> None:
        webhook = os.environ.get("DINGTALK_WEBHOOK")
        if not webhook:
            logger.info("未配置钉钉 Webhook (DINGTALK_WEBHOOK)，跳过推送。")
            return

        try:
            content = Path(report_path).read_text(encoding="utf-8")
            # 静默阻断占位报告不推送，避免钉钉噪音
            if "今日无新增实质性供应链断裂与合规风险" in content:
                logger.info("检测到静默阻断占位报告，跳过钉钉推送。")
                return
            if len(content) > 15000:
                content = content[:15000] + "\n\n> ⚠️ 报告过长，已自动截断。完整内容请查看源文件。"

            if mode == "weekly":
                first_line = "🔮【宏观合规战略】全球地缘与准入壁垒周报"
                ding_title = "🏛️ ESG 全球地缘与合规周报"
            else:
                first_line = "全球供应链合规与风险速递"
                ding_title = "🏛️ ESG 全球供应链动态日报 (Daily Intelligence)"
            ding_content = f"# {first_line}\n\n{content}"
            headers = {"Content-Type": "application/json"}
            data = {"msgtype": "markdown", "markdown": {"title": ding_title, "text": ding_content}}
            logger.info("正在向钉钉发送情报简报...")
            response = requests.post(webhook, headers=headers, data=json.dumps(data))
            logger.info(f"钉钉服务器返回: {response.text}")
        except Exception as exc:
            logger.error(f"钉钉推送失败: {exc}")

    # ── 入口 ─────────────────────────────────────────────

    def run(self, mode: str = "daily", report_path: str = "esg_global_report.md") -> None:
        t0 = time.monotonic()
        logger.info(f"═══ ESG Intelligence Agent v9 | Mode: {mode.upper()} ═══")

        # ── 重置 Token 统计 ─────────────────────────
        self._reset_token_stats()

        query_tasks = self.config.build_query_tasks(mode)
        logger.info(f"Query tasks: {len(query_tasks)} (mode={mode})")

        # ── Phase 0.5: AI 动态搜索词生成 ─────────────
        company_names = self.config.get_all_company_display_names()
        ai_discovery_urls: list[dict] = []
        try:
            discovery_queries = self._generate_ai_discovery_queries(mode, company_names)
            for i, dq in enumerate(discovery_queries):
                from urllib.parse import quote
                encoded = quote(dq, safe="")
                ai_url = (
                    f"https://news.google.com/rss/search?"
                    f"q={encoded}&hl=en-US&gl=US&ceid=US:en"
                )
                ai_discovery_urls.append({
                    "url": ai_url,
                    "source_id": f"ai_discovery_{i + 1}",
                })
            if ai_discovery_urls:
                logger.info(f"AI discovery: generated {len(ai_discovery_urls)} dynamic queries")
        except Exception as e:
            logger.warning(f"AI discovery query generation failed ({e}), continuing with static matrix only")

        # ── Phase 1: Sourcing Engine 三层供料 ─────────────
        engine = SourcingEngine()

        # 1a. 静态轨道（esg_sources.yaml）
        raw_items = engine.fetch_all_active_sources()

        # 1b. 动态任务矩阵（config.yaml query_tasks）— 修复死代码
        dynamic_urls: list[dict] = []
        for task in query_tasks:
            dynamic_urls.append({
                "url": task.url,
                "source_id": f"matrix_{task.track_label}_{task.lang}",
            })
        dynamic_items = engine.fetch_from_prebuilt_urls(dynamic_urls, time_window="24h")

        # 1c. AI 动态发现查询
        ai_items = engine.fetch_from_prebuilt_urls(ai_discovery_urls, time_window="24h")

        # 合并所有来源
        all_raw_items = raw_items + dynamic_items + ai_items
        logger.info(
            f"Phase 1 totals: {len(raw_items)} static + {len(dynamic_items)} dynamic + "
            f"{len(ai_items)} ai-discovery = {len(all_raw_items)} items"
        )

        for item in all_raw_items:
            pub_date_str = item.get("pub_date", "")
            parsed_date = None
            try:
                parsed_date = datetime.fromisoformat(pub_date_str)
            except (ValueError, TypeError):
                pass

            raw_title = str(item.get("title", ""))
            raw_url = str(item.get("link", ""))
            
            self.articles.append(NewsArticle(
                title=clean_title(raw_title) or raw_title,
                date=pub_date_str,
                source=str(item.get("source_id", "SourcingEngine")),
                url=normalize_url(raw_url) or raw_url,
                description=str(item.get("content", ""))[:300],
                company_name_zh="",
                company_name_en="",
                track_label=f"供料矩阵 ({item.get('source_id', '')})",
                lang="auto",
                topic_category="多源情报",
                parsed_date=parsed_date,
            ))
        logger.info(f"Phase 1: Sourcing Engine 返回了 {len(self.articles)} 条有效纯净数据。")

        if not self.articles:
            # FATAL 熔断前仍写入占位报告，明确系统已巡检
            self._send_system_alert(
                "🚨 [FATAL级警报] ESG雷达抓取源严重失效触发零数据熔断。"
                "今日抓取量为0，请立刻排查网络节点或RSS解析器状态！"
            )
            # 零数据时写入占位报告
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if mode == "weekly":
                title = "🏛️ ESG 全球地缘与合规周报 (Weekly Strategy Insight)"
                first_line = "🔮【宏观合规战略】全球地缘与准入壁垒周报"
            else:
                title = "🏛️ ESG 全球供应链动态日报 (Daily Intelligence)"
                first_line = "全球供应链合规与风险速递"
            placeholder = "\n".join([
                f"# {title}",
                "",
                f"> {first_line}",
                f"> 📅 **生成时间**: {now_str}",
                f"> 📊 **情报总数**: 0 条 | 涉及企业: 0 家",
                "",
                "---",
                "",
                "## 📑 今日无风险事件",
                "",
                "今日无新增实质性供应链断裂与合规风险。",
                "系统今日已成功巡检，所有供料轨道均无数据返回。",
                "",
                "---",
                "",
                "🤖 *本报告由 ESG Intelligence Agent 自动生成，数据来源于公开新闻源。*",
                "⚠️  *仅供决策参考，不构成投资或法律建议。*",
            ])
            Path(report_path).write_text(placeholder, encoding="utf-8")
            logger.info(f"零数据占位报告已写入: {report_path}")
            self._log_token_summary()
            return

        # ── Phase 2: Dedup by title + url (双重去重) ─────
        raw_count = len(self.articles)
        df_tmp = pd.DataFrame([a.__dict__ for a in self.articles])
        # Normalize URLs before dedup to merge UTM/tracking-parameter variants
        df_tmp["_norm_url"] = df_tmp["url"].apply(lambda u: normalize_url(u) if u else u)
        df_tmp = df_tmp\
            .drop_duplicates(subset=["title"], keep="first")\
            .drop_duplicates(subset=["_norm_url"], keep="first")
        df_tmp = df_tmp.drop(columns=["_norm_url"])
        self.articles = [NewsArticle(**row.to_dict()) for _, row in df_tmp.iterrows()]
        logger.info(f"Phase 2 dedup (title+norm_url): {raw_count} -> {len(self.articles)} articles")

        # ── Phase 3: Deep Content Extraction ────────────
        logger.info(f"Phase 3: Extracting body text from {len(self.articles)} articles...")
        extracted_count = 0
        for idx, article in enumerate(self.articles):
            body = ContentExtractor.extract(article.url)
            if body:
                article.raw_summary = body
                extracted_count += 1
            else:
                desc = article.description.strip()
                if desc and desc != article.title and len(desc) > 15:
                    article.raw_summary = desc[:200]
            if (idx + 1) % 10 == 0:
                logger.info(f"  Progress: {idx + 1}/{len(self.articles)}")
                time.sleep(0.3)

        logger.info(f"Phase 3 done. Body text extracted: {extracted_count}/{len(self.articles)}")

        # ── Phase 2.5: Entity Presence Filter ───────────
        pre_filter_count = len(self.articles)
        filtered_articles: list[NewsArticle] = []
        for article in self.articles:
            zh = article.company_name_zh
            en = article.company_name_en
            # 轨道 3（宏观政策）无企业名时直接保留
            if not zh and not en:
                filtered_articles.append(article)
            elif EntityFilter.passes(article, zh, en):
                filtered_articles.append(article)
            else:
                logger.info(f"[过滤] 未包含实体: {article.title}")
        self.articles = filtered_articles
        logger.info(f"Phase 2.5 entity filter: {pre_filter_count} -> {len(self.articles)}")

        if not self.articles:
            MarkdownReportWriter([], self.config, mode=mode).generate(report_path)
            return

        # ── Phase 2.6: Per-Company Throttle (漏斗限流) ──
        # 每家企业最多保留最新 20 条，12 家企业上限 240 条，防止数据管道雪崩
        pre_throttle = len(self.articles)
        MAX_PER_COMPANY = 20
        df_articles = pd.DataFrame([a.__dict__ for a in self.articles])
        # 构建企业分组键：优先 display name，无则用中文名+英文名
        df_articles["_company_key"] = df_articles.apply(
            lambda row: (
                f"{row.get('company_name_zh', '')} | {row.get('company_name_en', '')}"
                if row.get("company_name_zh") or row.get("company_name_en")
                else "宏观政策"
            ),
            axis=1,
        )
        # 按公司分组，每组按日期降序取最新 MAX_PER_COMPANY 条
        df_articles["_sort_dt"] = pd.to_datetime(
            df_articles.get("parsed_date", pd.Series(dtype=str)), errors="coerce"
        )
        df_throttled = (
            df_articles
            .sort_values(["_company_key", "_sort_dt"], ascending=[True, False], na_position="last")
            .groupby("_company_key", sort=False)
            .head(MAX_PER_COMPANY)
        )
        # 必须在实例化 NewsArticle 之前移除内部临时列，避免 TypeError
        df_throttled.drop(columns=["_company_key", "_sort_dt"], inplace=True, errors="ignore")
        self.articles = [NewsArticle(**row.to_dict()) for _, row in df_throttled.iterrows()]
        logger.info(
            f"Phase 2.6 per-company throttle (max {MAX_PER_COMPANY}/firm): "
            f"{pre_throttle} -> {len(self.articles)}"
        )

        # ── Phase 4: DeepSeek LLM Semantic Processing ────
        raw_data_list = []
        for article in self.articles:
            zh = article.company_name_zh
            en = article.company_name_en
            display_name = f"{zh} | {en}" if zh and en else (zh or en or "宏观政策")
            raw_data_list.append({
                "company_name_zh": zh,
                "company_name_en": en,
                "display_name": display_name,
                "title": article.title,
                "date": article.date,
                "source": article.source,
                "url": article.url,
                "raw_summary": article.raw_summary,
                "lang": article.lang,
                "track_label": article.track_label,
                "topic_category": article.topic_category,
                "parsed_date": article.parsed_date,
                "description": article.description,
            })

        all_v10_events = self.process_intelligence_with_llm(raw_data_list, mode)
        logger.info(f"Phase 4 done. LLM returned {len(all_v10_events)} total events (valid + invalid).")

        # ── Phase 5: Python 确定性渲染流水线 ─────────────
        intelligence_json = self._generate_v10_report_and_filter(all_v10_events, mode, report_path)
        logger.info(f"Phase 5 done. Python pipeline: {len(intelligence_json)} valid items -> {report_path}")

        elapsed = time.monotonic() - t0
        date_values = [item.get("date", "") for item in intelligence_json]
        dmin = min(date_values) if date_values else "?"
        dmax = max(date_values) if date_values else "?"
        logger.info(f"All done in {elapsed:.1f}s | {len(intelligence_json)} valid items | {dmin} ~ {dmax}")

        # ── Token 消耗摘要 + 追加到报告底部 ────────────
        token_summary = self._log_token_summary()
        try:
            token_footer = f"\n\n---\n\n{token_summary}\n"
            with open(report_path, "a", encoding="utf-8") as rf:
                rf.write(token_footer)
        except Exception:
            pass

        # ── Phase 6: 周度威胁态势审查 (weekly only) ──
        if mode == "weekly":
            try:
                self._weekly_threat_landscape_review(all_v10_events, report_path, token_summary)
            except Exception as e:
                logger.warning(f"Weekly threat review failed ({e}), weekly report unaffected")

    def __init__(self, config_path: str = None):
        self.config = AgentConfig.from_yaml(config_path)
        self.articles: list[NewsArticle] = []
        self._seen_urls: set[str] = set()
        self._cutoff = datetime.now(timezone.utc) - timedelta(days=self.config.days_limit)
        geo_count = len(self.config.geographical_tracks)
        prem_c = len(self.config.premium_company_tracks)
        prem_g = len(self.config.premium_global_tracks)
        daily_t = len(self.config.daily_topics)
        weekly_t = len(self.config.weekly_topics)
        logger.info(
            f"Loaded config: {len(self.config.companies)} companies | "
            f"{geo_count} geo tracks + {prem_c} premium company + {prem_g} premium global | "
            f"{daily_t} daily topics + {weekly_t} weekly topics | "
            f"cutoff: {self._cutoff.strftime('%Y-%m-%d')}"
        )


# ─────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ESG Intelligence Agent v9 — 双频动态播报",
    )
    parser.add_argument(
        "--mode",
        choices=["daily", "weekly"],
        default="daily",
        help="运行模式：daily=日常舆情（默认）/ weekly=宏观政策+全部主题",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径（默认: 脚本同目录下的 config.yaml）",
    )
    parser.add_argument(
        "--report",
        default="esg_global_report.md",
        help="报告输出路径（默认: esg_global_report.md）",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        default=False,
        help="跳过钉钉 Webhook 推送",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    agent = ESGIntelligenceAgent(config_path=args.config)
    agent.run(mode=args.mode, report_path=args.report)
    if not args.no_push:
        agent.push_to_dingtalk(mode=args.mode)
    else:
        logger.info("钉钉推送已跳过（--no-push）。")
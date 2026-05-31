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
os.environ["DEEPSEEK_API_KEY"] = "sk-6110382d87864de58b25ee87b6e06be6"
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
    def from_yaml(cls, path: str = "config.yaml") -> "AgentConfig":
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
1. 事件聚类：阅读所有新闻，将描述【同一核心事件】的多条新闻合并为一个独立事件，聚合所有相关来源的媒体名称和对应 URL。
2. 降噪判定 (is_valid_risk)：
   - 若实体错误（如"福特省长"、"福特医院"当成福特汽车），值为 false。
   - 若为正面或无实质风险事件（如减碳成功、引入机器人提升效率、正常商业投资），值为 false。
   - 只有明确包含物理停摆（关厂/减产）、劳资冲突（罢工/抗议）、质量与安全（召回/事故）、强力制裁等负面冲击时，值才为 true。
3. 严格分类 (risk_category) 与负面清单：
   - "供应链断裂预警"：仅限物理层面的供给中断（工厂因灾停产、矿端断供、核心供应商破产、物流瘫痪）。【负面清单】以下内容绝对不属于本类，必须归入 is_valid_risk=false 并直接丢弃：投资/股价波动、财报亏损、M&A/股权转让、需求疲软/销量下滑、新产品发布、技术合作、融资/增资。遇到这些话题时 risk_category 填入"无关噪音"且 is_valid_risk=false。
   - "市场准入预警"：仅限进出口禁令、关税惩罚、实体清单、强迫劳动货物扣留。【负面清单】绝对排除产品召回、质量事故、软件故障——这些归入"合规与运营危机"。
   - "合规与运营危机"：包含劳工罢工/抗议、重大安全事故（爆炸/矿难）、产品召回、车辆起火、软件安全缺陷、严重环保罚单。
   - "机构与声誉预警"：NGO指控、人权机构质询、评级下调等尚未演变为实质停产的高声誉风险事件。

# Executive Insight 生成规则（严格执行 — 华友钴业中心制）
1. 身份锚定：华友钴业是全球领先的新能源锂电上游材料供应商，主营前驱体（Precursor）与正极材料（Cathode Active Material），核心下游客户包括特斯拉、宝马、奔驰、大众等全球主机厂及电池制造商。所有 insight 必须从华友钴业的产业位置出发进行传导推演。
2. 结构铁律：每条 insight 必须严格遵循【客观事实 + 华友钴业视角传导分析】两段式结构。绝对禁止出现任何形式的"建议""应当""需要""可考虑"等行为指导性措辞。
3. 传导分析强制切入点（至少覆盖以下一个维度）：
   - 订单冲击：下游主机厂客户的危机事件是否会影响其对华友前驱体/正极材料的采购订单量、交付节奏或定价条款。
   - 供应链连续性：上游矿端/中游制造环节的断供、停产、物流中断是否会影响华友的原料保障或产成品交付。
   - 海外项目准入合规：目标市场（欧盟、北美、东南亚）的监管政策变化是否会影响华友海外项目的环评审批、出口许可或供应链合规认证（如 CSRD/CSDDD/EU Battery Regulation）。
4. 字数红线：50-80 汉字或英文单词，低于 50 或超过 80 视为违规。
5. 禁止废话：严禁使用"可能影响运营""面临声誉风险""需持续关注""建议华友"等空洞套话。必须指明具体的传导环节和波及路径。
6. 正例（合规）：
   - "宝马韩国市场因发动机起火被禁售，触发韩国《汽车管理法》召回程序。华友作为宝马电池材料上游供应商，需关注该车型所涉电池型号是否与华友正极材料供应体系存在关联，韩国市场禁售可能导致该车型减产，间接影响华友对韩系电池厂的正极材料出货排期。"
   - "特斯拉瑞典维修工人罢工规模虽缩减，但 IF Metall 工会仍维持封锁。北欧市场劳资冲突的持续发酵可能加速主机厂对供应链人权合规的审查力度，华友在印尼镍矿项目的劳工标准及出海合规文档将面临更严苛的欧盟 CSDDD 穿透审计。"
7. 反例（违规，绝不可输出）：
   - "可能影响运营" — 空洞无物，违反规则5。
   - "面临声誉风险" — 未说明传导链条，违反规则2。
   - "建议华友加强合规管理" — 包含行为建议，违反规则2（华友视角只推演传导，不给出建议）。

# Output Format
你必须仅输出合法的 JSON 数据，不得包含任何 Markdown 标记或额外解释。JSON 结构必须如下：
{{
  "events": [
    {{
      "entity": "企业全称（必须精确匹配目标企业列表中某一项）",
      "core_event_title": "一句话概括合并后的核心事件",
      "executive_insight": "客观事实 + 华友钴业视角传导分析，50-80字",
      "date": "最新日期 YYYY-MM-DD",
      "sources": [{{"name": "媒体A", "url": "https://example.com/articleA"}}, {{"name": "媒体B", "url": "https://example.com/articleB"}}],
      "risk_category": "上述四大分类之一",
      "is_valid_risk": true
    }},
    {{
      "entity": "亨利·福特医院",
      "core_event_title": "亨利·福特医院发生罢工",
      "executive_insight": "实体错误，非监控目标",
      "date": "2026-05-27",
      "sources": [{{"name": "Jacobin", "url": "https://jacobin.com/example"}}],
      "risk_category": "机构与声誉预警",
      "is_valid_risk": false
    }}
  ]
}}

重要：is_valid_risk 为 false 的条目也必须输出，以便审计追踪。所有新闻（包括被判定为无效的）都必须在 events 数组中占一条记录，通过 is_valid_risk 字段区分。
sources 字段中的每个元素必须包含 name（媒体名称）和 url（新闻原文直链，优先使用输入数据中提供的已解密 URL）。
如果没有收到任何新闻，请返回 {{ "events": [] }}。"""

    # 分批处理常量：每批最多发送的文章数
    BATCH_SIZE = 15

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

    @classmethod
    def _generate_v10_report_and_filter(cls, all_events: list[dict], mode: str, report_path: str = "esg_global_report.md") -> list[dict]:
        """Python 确定性渲染流水线：过滤 → 分组 → 生成 Markdown 报告。

        所有降噪逻辑由 Python if-else 物理执行，不依赖 LLM 判断。
        Returns: v9 兼容的 dict 列表，用于下游日志统计。
        """
        # ── 1. 确定性降噪（Python 物理隔绝） ──
        invalid_events: list[dict] = []
        valid_events: list[dict] = []
        for event in all_events:
            if not isinstance(event, dict):
                continue
            if event.get("is_valid_risk") is False:
                invalid_events.append(event)
            else:
                valid_events.append(event)

        # 审计日志
        for e in invalid_events:
            logger.info(f"[v10 降噪] 已过滤: {e.get('entity', '?')} | {e.get('core_event_title', '?')[:60]}")
        logger.info(f"Python 降噪: {len(invalid_events)} invalid -> dropped, {len(valid_events)} valid -> report")

        # ── 2. 按风险类别分组 ──
        categorized: dict[str, list[dict]] = {
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
            title = "📊 ESG 全球供应链动态日报"
            first_line = "🚨【突发舆情雷达】日常合规与风险速递"

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
            "供应链断裂预警": "供应链断裂预警",
            "政策与市场准入": "政策与市场准入",
            "合规与运营危机": "合规与运营危机",
            "机构与声誉预警": "机构与声誉预警",
        }
        category_descriptions = {
            "供应链断裂预警": "仅限物理层面的供给中断（工厂停产、物流瘫痪、核心供应商断供）",
            "政策与市场准入": "仅限引发无法卖货/买货的事件（实体清单、关税惩罚、进出口禁令、强迫劳动扣留）",
            "合规与运营危机": "劳工罢工、重大安全事故、产品召回、严重环保罚单引发的即期运营阻断",
            "机构与声誉预警": "NGO指控、人权机构质询、评级下调等尚未演变为实质停产的高声誉风险事件",
        }

        # 目录
        for cat_key, cat_name in category_cn_names.items():
            evs = categorized.get(cat_key, [])
            if not evs:
                continue
            lines.append(f"- **【{cat_name}】**（{len(evs)} 条）")
            entity_counts: dict[str, int] = {}
            for e in evs:
                ent = str(e.get("entity", "")).strip()
                entity_counts[ent] = entity_counts.get(ent, 0) + 1
            for ent in sorted(entity_counts.keys()):
                lines.append(f"  - {ent}: {entity_counts[ent]} 条")
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

            for e in evs:
                entity = str(e.get("entity", "")).strip()
                title_text = str(e.get("core_event_title", "")).strip()
                insight = str(e.get("executive_insight", "")).strip()
                date = str(e.get("date", ""))[:10]
                # 信息源聚合：渲染为 [媒体名](解密URL) 带超链接格式
                sources_raw = e.get("sources", [])
                source_links: list[str] = []
                if isinstance(sources_raw, list):
                    for s in sources_raw:
                        if isinstance(s, dict):
                            name = str(s.get("name", "")).strip()
                            src_url = str(s.get("url", "")).strip()
                            if name:
                                # 二次解密：LLM 可能回传未解密的 Google News URL
                                if src_url and "news.google.com" in src_url:
                                    src_url = resolve_news_url(src_url)
                                if src_url and src_url.lower().startswith("http"):
                                    source_links.append(f"[{name}]({src_url})")
                                else:
                                    source_links.append(name)
                        elif isinstance(s, str) and s.strip():
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
            "🤖 *本报告由 ESG Intelligence Agent 驱动，严格遵循「LLM JSON 提取 -> Python 确定性降噪 -> 模板渲染」三级清洗管线。*",
            "⚠️  *数据来源为公开 RSS 新闻源，仅供决策参考，不构成投资或法律建议。*",
        ]

        report_md = "\n".join(lines)
        Path(report_path).write_text(report_md, encoding="utf-8")
        logger.info(f"v10 report written: {report_path} ({len(valid_events)} valid / {len(invalid_events)} invalid)")

        # 返回 v9 兼容格式用于 run() 日志统计
        compatible: list[dict] = []
        for e in valid_events:
            sources_raw = e.get("sources", [])
            if isinstance(sources_raw, list):
                source_names = []
                for s in sources_raw:
                    if isinstance(s, dict):
                        source_names.append(str(s.get("name", "")))
                    elif isinstance(s, str):
                        source_names.append(s)
                source_str = ", ".join(n for n in source_names if n)
            else:
                source_str = str(sources_raw) if sources_raw else "Unknown"
            risk_cat = str(e.get("risk_category", "")).strip()
            compatible.append({
                "company": str(e.get("entity", "")).strip(),
                "title": str(e.get("core_event_title", "")).strip(),
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

    # ── 钉钉推送 ──────────────────────────────────────────

    def push_to_dingtalk(self, report_path: str = "esg_global_report.md", mode: str = "daily") -> None:
        webhook = os.environ.get("DINGTALK_WEBHOOK")
        if not webhook:
            logger.info("未配置钉钉 Webhook (DINGTALK_WEBHOOK)，跳过推送。")
            return

        try:
            content = Path(report_path).read_text(encoding="utf-8")
            if len(content) > 15000:
                content = content[:15000] + "\n\n> ⚠️ 报告过长，已自动截断。完整内容请查看源文件。"

            if mode == "weekly":
                first_line = "🔮【宏观合规战略】全球地缘与准入壁垒周报"
                ding_title = "🏛️ ESG 全球地缘与合规周报"
            else:
                first_line = "🚨【突发舆情雷达】日常合规与风险速递"
                ding_title = "📊 ESG 全球供应链动态日报 (Daily Risk Radar)"
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

        query_tasks = self.config.build_query_tasks(mode)
        logger.info(f"Query tasks: {len(query_tasks)} (mode={mode})")

        # ── Phase 1: RSS Fetch ───────────────────────────
        skipped = 0
        for idx, q in enumerate(query_tasks, 1):
            logger.info(f"[{idx:>4}/{len(query_tasks)}] [{q.track_label}] {q.url[:80]}...")
            for raw in NewsFetcher.fetch(q.url):
                parsed_date = parse_rss_date(raw["date"])
                if parsed_date and parsed_date < self._cutoff:
                    skipped += 1
                    continue
                if raw["url"] in self._seen_urls:
                    continue
                self._seen_urls.add(raw["url"])
                self.articles.append(NewsArticle(
                    title=raw["title"], date=raw["date"],
                    source=raw["source"], url=raw["url"],
                    description=raw["description"],
                    company_name_zh=q.company_name_zh,
                    company_name_en=q.company_name_en,
                    track_label=q.track_label, lang=q.lang,
                    topic_category=q.topic_category,
                    parsed_date=parsed_date,
                ))
            time.sleep(1.2)

        logger.info(f"Phase 1 done. Skipped {skipped} old. Collected {len(self.articles)} articles.")

        if not self.articles:
            MarkdownReportWriter([], self.config, mode=mode).generate(report_path)
            return

        # ── Phase 2: Dedup by title + url (双重去重) ─────
        raw_count = len(self.articles)
        df_tmp = pd.DataFrame([a.__dict__ for a in self.articles])\
            .drop_duplicates(subset=["title"], keep="first")\
            .drop_duplicates(subset=["url"], keep="first")
        self.articles = [NewsArticle(**row.to_dict()) for _, row in df_tmp.iterrows()]
        logger.info(f"Phase 2 dedup (title+url): {raw_count} -> {len(self.articles)} articles")

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

    def __init__(self, config_path: str = "config.yaml"):
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
        default="config.yaml",
        help="配置文件路径（默认: config.yaml）",
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
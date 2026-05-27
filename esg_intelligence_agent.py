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

    @staticmethod
    def _render_item(row: pd.Series) -> list[str]:
        company = str(row.get("company", "")).strip()
        title   = str(row.get("title", row.get("title_cn", ""))).strip()
        url     = str(row.get("url", "")).strip()
        insight = str(row.get("insight", "")).strip()
        source  = str(row.get("source", "Unknown"))[:50].strip()
        date_s  = str(row.get("date", ""))[:10]

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
        """构建 DeepSeek System Prompt - v9 四重标签审计版。"""
        companies_str = "\n".join(f"  - {name}" for name in company_names)
        mode_hint = (
            "weekly 模式：同时关注宏观政策与地缘合规动态。"
            if mode == "weekly"
            else "daily 模式：聚焦日常舆情（劳工、污染、事故、社区冲突）。"
        )
        return f"""你是一个顶级的 ESG 供应链风险分析师。你的核心任务是【实体消歧】、【风险提炼】与【四重标签审计】。

## 当前运行模式
{mode_hint}

## 目标企业列表（唯一有效实体）
{companies_str}

## 警告：搜索结果中包含大量重名噪音
例如：搜索中国新能源企业"格林美 GEM"，会出现假肢品牌 GEM、法国互助组织 GEM、珠宝宝石 (gem)、隐藏的宝石 (hidden gem) 等完全无关的内容。
你必须根据新闻标题、正文摘要、语种和来源媒体，结合新能源/电池材料/钴锂镍矿产行业背景进行严格甄别。

## 你的处理逻辑（必须严格遵循）

### 步骤 1：实体消歧
仔细甄别每一条新闻的主体：
- 如果新闻与上述目标企业（特别是新能源、电池材料行业）**毫无关系**，**请直接将该条目丢弃，不要将其包含在输出 JSON 中**。
- 例如：假肢品牌 GEM Prosthetics、法国互助组织/协会 GEM、宝石/珠宝类新闻、与矿产/电池/新能源完全无关的 GEM 缩写等 → 统统丢弃。
- 例如：搜索 CATL（宁德时代）时出现的完全不相关的缩写 CATL → 丢弃。

### 步骤 2：噪音过滤（一票否决）⚠️
**【绝对过滤指令】**：如果该新闻的核心内容是以下任意一类，**请直接将其丢弃（判定为无效噪音）！绝不要输出这些内容！**：
- 📉 **股票涨跌**：股价波动、盘中异动、技术分析、资金流向、涨停/跌停
- 📊 **基金持仓**：基金增减持、仓位调整、ETF 成分股变动
- 💰 **股息派发**：分红方案、除权除息、股利发放
- 📝 **常规研报评级**：券商买入/卖出/持有评级、目标价调整（除非研报内容涉及地缘政治或合规风险）
- 🏢 **企业内部常规会议**：主题党日、研学活动、党建、年会、团建、内部培训
- 🤝 **常规战略签约**：未涉及出海限制、跨境合规风险或制裁风险的普通商业合作签约
- 📢 **产品广告/营销**：纯产品发布（无召回/质量丑闻）、品牌营销活动
- 💹 **纯财报数据**：营收、利润、毛利率等常规财务指标（无 ESG 风险关联）

我们只关心真实的**运营风险**（罢工/事故/污染/社区冲突）与**宏观合规壁垒**（法案/禁令/制裁/出口管制/供应链强制要求）。

### 步骤 3：四重标签审计（强制打标）
在输出每一条情报时，必须在 tags 数组中打上适用的标签（可多标签）：

**\u3010机构预警\u3011** — 判定条件：原文链接 URL 包含 `business-humanrights.org`。
   → 含义：商业与人权资源中心（BHRRC）定向抓取到的人权/劳工黑历史记录。属于高可信度机构预警。

**\u3010政策前沿\u3011** — 判定条件：原文链接 URL 包含 `efrag.org`。
   → 含义：欧盟财务报告咨询组（EFRAG）发布的 CSRD/ESRS/CSDDD 等合规准则更新。属于顶层政策变动信号。

**\u3010市场准入预警\u3011** — 判定条件：新闻内容涉及美国 IRA（通胀削减法案）、FEOC（外国敏感实体规则）、UFLPA（涉疆法案）、实体清单、出口管制，或欧盟电池法案、电池护照、CRMA、CBAM（碳边境调节）、CSRD/CSDDD 供应链尽职调查。
   → 重点提炼：对企业出海补贴资格、合规成本、市场准入资格的封锁或限制影响。

**\u3010供应链断裂预警\u3011** — 判定条件：新闻内容涉及印尼"下游化"（Hilirisasi）、原矿出口禁令、本地化加工强制要求，或非洲/其他资源国矿产主权、禁止原矿出口政策。
   → 重点提炼：对上游原材料供应的断供风险、成本上升压力、本地化建厂的强制要求。

**重要**：
- 所有情报条目都必须至少打上一个标签。标签存放在 tags 数组中（字符串数组）。
- 例如：tags = ["\u3010供应链断裂预警\u3011", "\u3010市场准入预警\u3011"]
- 如果无法确定标签，默认使用 ["\u3010供应链合规\u3011"] 作为 fallback。

### 步骤 4：风险提炼
对于判定为真实目标企业的情报：
- 赋予一个**通俗专业的业务分类**（如：劳工权益、环境污染、供应链合规、社区冲突、安全事故、可持续发展、监管政策、市场准入、资源民族主义）。
- **严禁使用任何布尔逻辑词作为分类**！禁止在分类中出现 "OR"、"AND"、"罢工 OR 抗议" 等搜索关键词。
- 如果新闻内容虽提及目标企业但无实质性 ESG 风险信息，也请遵照步骤 2 的噪音过滤规则剔除。

### 步骤 5：高管洞察撰写
提炼 ≤50 字的中文高管洞察摘要（纯洞察文本，不含标签前缀），必须明确包含：时间、地点、涉事主体、风险定级。
- **叙事风格要求**：采用多样化、简练的商业简报叙事风格。像专业的顶级商业调查分析师一样，一语道破核心事件与风险定级，语言自然、犀利且富有穿透力。
- **坚决避免机械的句式模板**：绝不要每条都以"xxxx年x月，[企业名]..."这类刻板句式开头。请灵活变换表达方式，让每条摘要都具有独立的阅读价值。
- **注意**：insight 字段仅包含纯文本洞察，标签已通过 tags 字段单独提供，不要在 insight 中重复标签前缀。

### 步骤 6：标题翻译
将非中文原始标题准确翻译为简体中文，填入 title 字段。

## 强制输出格式
请严格返回纯 JSON 数组（不要包含任何 markdown 代码块符号如 ```json），JSON 结构：
[
  {{
    "company": "企业全称（必须精确匹配目标企业列表中某一项）",
    "risk_category": "通俗业务分类（如：劳工权益、环境污染、供应链合规、市场准入、资源民族主义）",
    "tags": ["【机构预警】", "【市场准入预警】"],
    "title": "准确完整的中文标题（原文标题的简体中文翻译）",
    "insight": "≤50字的高管洞察摘要（纯文本，不含标签前缀。例如：印尼镍矿禁令升级，华友钴业面临本地化建厂硬性约束，存在工期延误与成本超支风险。）",
    "source": "来源媒体名称",
    "date": "YYYY-MM-DD 格式日期",
    "url": "原文链接"
  }}
]

如果没有符合条件的有效情报，请返回空数组 []。"""

    # 分批处理常量：每批最多发送的文章数
    BATCH_SIZE = 15

    @classmethod
    def _extract_json_array(cls, text: str) -> Optional[list]:
        """从 LLM 回复中强行提取合法 JSON 数组并校验。

        1. 使用正则 r'\\[.*\\]' (DOTALL) 提取最外层的 JSON 数组结构。
        2. json.loads 解析。
        3. 校验：必须是非空 list，其中至少一条包含 "company" 字段且非空。

        Returns:
            解析成功返回 list[dict]，失败返回 None。
        """
        if not text or not text.strip():
            return None
        # 步骤1: 正则提取最外层 JSON 数组
        match = re.search(r"\[.*\]", text.strip(), re.DOTALL)
        if not match:
            logger.warning("_extract_json_array: no JSON array found in LLM response.")
            return None
        candidate = match.group(0)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as e:
            logger.warning(f"_extract_json_array: JSON parse failed: {e}")
            return None
        # 步骤2: 结构校验
        if not isinstance(parsed, list):
            logger.warning("_extract_json_array: parsed result is not a list.")
            return None
        if len(parsed) == 0:
            # 空数组是合法回复（无有效情报）
            return []
        # 步骤3: 至少包含一条有效企业名
        has_company = any(
            isinstance(item, dict) and str(item.get("company", "")).strip()
            for item in parsed
        )
        if not has_company:
            logger.warning("_extract_json_array: parsed array has no items with non-empty 'company' field — discarding.")
            return None
        return parsed

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
        """分批将文章发送至 DeepSeek，进行语义降噪与四重标签审计。

        ═══ 核心改动（反降级重构） ═══
        • BATCH_SIZE=15 分批处理，防止单次数据量过大导致 JSON 崩溃。
        • 使用 _extract_json_array() 正则强提取 + 企业名校验。
        • 批次失败直接丢弃该批次数据，绝不回退到原始数据。
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
            f"LLM processing: {total} articles → {batch_count} batch(es) "
            f"(BATCH_SIZE={self.BATCH_SIZE}, mode={mode})"
        )

        all_results: list[dict] = []
        client = OpenAI(api_key=api_key, base_url=self.DEEPSEEK_BASE_URL)

        for batch_idx in range(batch_count):
            start = batch_idx * self.BATCH_SIZE
            end = min(start + self.BATCH_SIZE, total)
            batch = raw_data_list[start:end]
            logger.info(f"  Batch {batch_idx + 1}/{batch_count}: articles #{start + 1}–{end} ({len(batch)} items)")

            user_message = self._build_articles_text(batch, start)

            try:
                response = client.chat.completions.create(
                    model=self.DEEPSEEK_MODEL,
                    messages=[
                        {"role": "system", "content": self._build_system_prompt(company_names, mode)},
                        {"role": "user", "content": user_message},
                    ],
                    temperature=0.1,
                    max_tokens=8192,
                )

                raw_output = response.choices[0].message.content or ""
                logger.info(f"  LLM response received ({len(raw_output)} chars).")

                parsed = self._extract_json_array(raw_output)
                if parsed is None:
                    logger.warning(
                        f"  ⚠ Batch {batch_idx + 1} failed JSON extraction/validation — "
                        f"DISCARDING {len(batch)} articles to prevent noise pollution."
                    )
                    continue

                all_results.extend(parsed)
                logger.info(f"  Batch {batch_idx + 1}: {len(parsed)} intelligence items extracted.")

            except Exception as e:
                logger.warning(
                    f"  ⚠ Batch {batch_idx + 1} LLM call failed ({type(e).__name__}: {e}) — "
                    f"DISCARDING {len(batch)} articles to prevent noise pollution."
                )
                continue

        logger.info(f"LLM processing complete: {len(all_results)} total intelligence items from {batch_count} batch(es).")
        return all_results

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

    def run(self, mode: str = "daily") -> None:
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
            MarkdownReportWriter([], self.config, mode=mode).generate()
            return

        # ── Phase 2: Dedup by title ──────────────────────
        raw_count = len(self.articles)
        df_tmp = pd.DataFrame([a.__dict__ for a in self.articles]).drop_duplicates(subset=["title"], keep="first")
        self.articles = [NewsArticle(**row.to_dict()) for _, row in df_tmp.iterrows()]
        logger.info(f"Phase 2 dedup: {raw_count} → {len(self.articles)} articles")

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
        logger.info(f"Phase 2.5 entity filter: {pre_filter_count} → {len(self.articles)}")

        if not self.articles:
            MarkdownReportWriter([], self.config, mode=mode).generate()
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
        self.articles = [NewsArticle(**row.to_dict()) for _, row in df_throttled.iterrows()]
        # 清理临时列
        for col in ["_company_key", "_sort_dt"]:
            if col in df_throttled.columns:
                df_throttled.drop(columns=[col], inplace=True)
        logger.info(
            f"Phase 2.6 per-company throttle (max {MAX_PER_COMPANY}/firm): "
            f"{pre_throttle} → {len(self.articles)}"
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

        intelligence_json = self.process_intelligence_with_llm(raw_data_list, mode)
        logger.info(f"Phase 4 done. LLM returned {len(intelligence_json)} intelligence items.")

        # ── Phase 5: Report ──────────────────────────────
        report_path = "esg_global_report.md"
        MarkdownReportWriter(intelligence_json, self.config, mode=mode).generate(report_path)

        elapsed = time.monotonic() - t0
        date_values = [item.get("date", "") for item in intelligence_json]
        dmin = min(date_values) if date_values else "?"
        dmax = max(date_values) if date_values else "?"
        logger.info(f"All done in {elapsed:.1f}s | {len(intelligence_json)} items | {dmin} ~ {dmax}")

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
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    agent = ESGIntelligenceAgent(config_path=args.config)
    agent.run(mode=args.mode)
    agent.push_to_dingtalk(mode=args.mode)

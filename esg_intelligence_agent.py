#!/usr/bin/env python3
"""
ESG 情报监控智能体 v6 — 高级搜索语法 + 实体出现校验（抗噪升级）
═══════════════════════════════════════════════════════════════════════════════
架构说明
────────
• AgentConfig.from_yaml()        — 读取多维字典结构的 config.yaml，含 negative_filters
• AgentConfig.build_query_matrix() — 生成带精确引号+负面词的高级搜索查询矩阵
• EntityFilter.passes()          — 【新】实体出现校验：正文/标题必须含公司短名
• NewsFetcher                    — Google RSS / Bing RSS 双通道抓取
• ContentExtractor               — 向原始 URL 深度抓取正文前 200 字
• TranslationEngine              — deep_translator GoogleTranslator 自动翻译非中文内容
• MarkdownReportWriter           — 严格分层 Markdown：企业全称 H1 → 主题 H2 → 新闻 H3
═══════════════════════════════════════════════════════════════════════════════
v6 新增
───────
1. 高级搜索语法（源头过滤）
   查询格式：`"{公司短名}" {主题词} {当前语言负面词}`
   例：`"Huayou Cobalt" (strike OR protest) -stock -shares`
   负面词从 config.yaml 新增字段 negative_filters.{lang} 读取；
   若该字段缺失，则静默跳过（向后兼容旧版 config.yaml）。

2. 实体出现校验（本地后置过滤，Phase 2.5）
   深度抓取正文后，检查公司短名（zh 或 en）是否出现于
   原始标题 或 raw_summary（忽略大小写）。
   不通过者直接丢弃，并打印：[过滤] 未包含实体: {标题}

YAML 结构注意事项
─────────────────
topics 列表中 rmi 条目存在两个 `id:` 字段（YAML 规范不允许重复键，
safe_load 会以后一个值覆盖前一个）。本脚本在解析时对此做防御性处理：
  - 企业 id 从 `id` 字段读取（唯一）
  - 主题语言文本按 zh/en/id/fr 字段读取，不依赖 `id` 字段做语言路由
"""

import html
import json
import logging
import os
import re
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
from deep_translator import GoogleTranslator

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

# 每种语言对应 Google News RSS 的 hl/gl 参数，以及报告中显示的标签
LANG_CONFIG: dict[str, dict] = {
    "zh": {"hl": "zh-CN", "gl": "CN",  "label": "中文"},
    "en": {"hl": "en",    "gl": "US",  "label": "English"},
    "id": {"hl": "id",    "gl": "ID",  "label": "Bahasa Indonesia"},
    "fr": {"hl": "fr",    "gl": "FR",  "label": "Français"},
}

# 中文语言字段名 → 搜索时使用 short_name_zh；其余语言使用 short_name_en
LANG_TO_COMPANY_FIELD: dict[str, str] = {
    "zh": "short_name_zh",
    "en": "short_name_en",
    "id": "short_name_en",   # 印尼语搜索用英文公司名效果更佳
    "fr": "short_name_en",   # 法语同上
}

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
    """单条搜索任务"""
    company_id: str    # 公司 id（如 "huayou"）
    topic_zh: str      # 主题中文标签（用于报告分组，如 "RMI 负责任矿产"）
    query: str         # 最终搜索串（如 "华友钴业 RMI 负责任矿产"）
    lang: str          # 语言代码（如 "zh"）


@dataclass
class NewsArticle:
    title: str = ""
    date: str = ""
    source: str = ""
    url: str = ""
    description: str = ""
    company_id: str = ""
    topic_zh: str = ""
    lang: str = ""
    parsed_date: Optional[datetime] = None
    raw_summary: str = ""           # 深度抓取的原文正文片段
    translated_title: str = ""      # 翻译后标题（非中文文章才有值）
    translated_summary: str = ""    # 翻译后摘要（非中文文章才有值）


@dataclass
class AgentConfig:
    """
    从 config.yaml 解析的完整配置。
    companies / topics 保存原始字典列表，运行时通过 _company_lookup 加速。

    新增字段 negative_filters（dict，键为语言代码）
    ─────────────────────────────────────────────
    config.yaml 示例：
        negative_filters:
          zh: "-股票 -股价 -涨停 -A股"
          en: "-stock -shares -investor -dividend"
          id: "-saham -bursa"
          fr: "-action -bourse -dividende"

    若 config.yaml 中未定义 negative_filters（旧版兼容），则所有语言返回空字符串。
    """
    companies: list[dict] = field(default_factory=list)
    topics: list[dict] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    days_limit: int = 14
    negative_filters: dict[str, str] = field(default_factory=dict)
    _company_lookup: dict[str, dict] = field(default_factory=dict, repr=False)

    # ── 工厂方法 ──────────────────────────────────────────

    @classmethod
    def from_yaml(cls, path: str = "config.yaml") -> "AgentConfig":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        cfg = cls(
            companies=raw.get("companies", []),
            topics=raw.get("topics", []),
            languages=raw.get("languages", ["zh", "en"]),
            days_limit=raw.get("days_limit", 14),
            negative_filters=raw.get("negative_filters", {}),
        )
        cfg._build_company_lookup()
        return cfg

    def _build_company_lookup(self) -> None:
        """构建 company_id → dict 快速查找表。"""
        for company in self.companies:
            cid = company.get("id", "").strip()
            if cid:
                self._company_lookup[cid] = company

    # ── 查询工具 ──────────────────────────────────────────

    def get_full_display_name(self, company_id: str) -> str:
        """
        返回企业全称用于报告一级标题展示：
            浙江华友钴业股份有限公司 | Zhejiang Huayou Cobalt Co., Ltd.
        """
        c = self._company_lookup.get(company_id, {})
        zh = c.get("full_name_zh") or c.get("short_name_zh") or company_id
        en = c.get("full_name_en") or c.get("short_name_en") or company_id
        return f"{zh} | {en}"

    def get_entity_names(self, company_id: str) -> tuple[str, str]:
        """
        返回用于实体校验的公司短名（zh, en）。
        实体校验时只需短名，全称不用于子串匹配（防止误判）。
        """
        c = self._company_lookup.get(company_id, {})
        return (
            c.get("short_name_zh", company_id),
            c.get("short_name_en", company_id),
        )

    # ── 查询矩阵构建 ───────────────────────────────────────

    def build_query_matrix(self) -> list["QueryItem"]:
        """
        遍历 companies × topics × languages 生成搜索任务列表。

        查询串格式（v6 高级搜索语法）
        ──────────────────────────────
          "{公司短名}" {主题词} {当前语言负面词}

        示例：
          "Huayou Cobalt" (strike OR protest) -stock -shares
          "华友钴业" RMI 负责任矿产 -股票 -股价

        规则说明
        ────────
        ① 公司短名用双引号包裹 → 搜索引擎强制精确匹配，防止模糊扩散
           zh 语言 → short_name_zh；en/id/fr → short_name_en
        ② 主题词：topic 字典对应 lang 字段，fallback 链 lang→en→zh→id→fr
        ③ 负面词：negative_filters[lang]；缺失时为空字符串（向后兼容）
        ④ 主题展示名（报告分组 key）：永远取 topic["zh"]
        """
        items: list[QueryItem] = []

        for company in self.companies:
            cid           = company.get("id", "").strip()
            short_name_zh = company.get("short_name_zh", cid)
            short_name_en = company.get("short_name_en", cid)

            for topic in self.topics:
                topic_zh_label = topic.get("zh") or topic.get("en") or str(topic.get("id", ""))

                for lang in self.languages:
                    # ① 公司短名（加双引号实现精确匹配）
                    raw_name = short_name_zh if lang == "zh" else short_name_en
                    if not raw_name:
                        raw_name = cid
                    quoted_name = f'"{raw_name}"'

                    # ② 主题关键词（fallback 链）
                    topic_keyword = ""
                    for key in [lang, "en", "zh", "id", "fr"]:
                        val = topic.get(key, "")
                        if val:
                            topic_keyword = val
                            break
                    if not topic_keyword:
                        topic_keyword = topic_zh_label

                    # ③ 负面词（来自 negative_filters，缺失时静默跳过）
                    neg_terms = self.negative_filters.get(lang, "").strip()

                    # ④ 拼接最终查询串
                    parts = [quoted_name, topic_keyword]
                    if neg_terms:
                        parts.append(neg_terms)
                    query_str = " ".join(parts).strip()

                    items.append(QueryItem(
                        company_id=cid,
                        topic_zh=topic_zh_label,
                        query=query_str,
                        lang=lang,
                    ))

        return items


# ─────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────

def strip_html(raw: str) -> str:
    """去除 HTML 标签与实体，返回纯文本。"""
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
    """解析 RFC-2822 格式的 RSS pubDate，返回带时区的 datetime 或 None。"""
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
    """将 datetime / 字符串统一格式化为 YYYY-MM-DD。"""
    if isinstance(raw, datetime):
        return raw.strftime("%Y-%m-%d")
    try:
        return pd.to_datetime(str(raw), utc=True).strftime("%Y-%m-%d")
    except Exception:
        return str(raw)[:10]



# ─────────────────────────────────────────────────────────
# 实体出现校验器（v6 新增）
# ─────────────────────────────────────────────────────────

class EntityFilter:
    """
    验证新闻标题或正文摘要中是否真实出现了目标公司名称。

    v7 升级：Regex 边界匹配 + 智能大小写敏感策略 + CJK 专项处理
    ─────────────────────────────────────────────────────────────
    问题根源
        原版使用纯字符串 `.lower() in haystack`，对短缩词（GEM / CATL / CMOC）
        会命中无关单词内的片段，例如：
          "hidden gem"  → "gem" ⊂ haystack          → 误判通过  ❌
          "management"  → "gem" ⊂ "mana**gem**ent"  → 误判通过  ❌

    修复策略
    ────────
    ① 单词边界（\\b）—— 仅用于纯 ASCII alias
       英文/数字 alias 使用 re.search(r'\\b{alias}\\b', haystack)，
       确保只命中完整独立词，而非其他单词的子片段。
       "hidden gem" → \\bGEM\\b（严格大小写）→ 无匹配 → 丢弃 ✅

    ② 中文/CJK alias —— 不加 \\b，直接子串匹配
       Python 的 \\b 基于 ASCII \\w=[a-zA-Z0-9_] 定义单词边界；
       中文字符属于非 \\w 字符。在全中文文本中，\\b格林美\\b 这样的
       模式在所有位置两侧都是非\\w，导致零宽断言异常，实测不匹配。
       解决方案：检测到 alias 含 CJK 字符时，使用 re.search(escaped, haystack)
       不加 \\b，直接匹配即可。中文词组本身不会产生英文那种子片段问题。

    ③ 智能大小写（Smart Case）
       判断条件：alias.isupper() and alias.isascii() and len(alias) <= 5
       → True  （如 "GEM", "CMOC", "CATL", "RMI", "CNGR"）
               严格区分大小写（无 re.IGNORECASE）。
               只有大写 GEM 命中，小写 gem / Gem 被过滤。
       → False （如 "Huayou Cobalt", "Ganfeng", "华友钴业", "格林美"）
               不区分大小写（re.IGNORECASE）。

    ④ 正则预编译缓存（_PATTERN_CACHE）
       同一 alias 在整次运行中只编译一次，避免循环中重复调用 re.compile()。

    典型案例
    ────────
    alias="GEM", text="hidden gem necklace"
        → strict + \\b → r'\\bGEM\\b'（大小写敏感）→ 无匹配 → 丢弃 ✅
    alias="GEM", text="GEM Co., Ltd. recycling report"
        → strict + \\b → 命中大写 GEM → 保留 ✅
    alias="GEM", text="management gem strategy"
        → strict + \\b → "gem" 小写，严格大小写 → 无匹配 → 丢弃 ✅
    alias="CATL", text="catl new factory plan"
        → strict + \\b → 大小写敏感 → 无匹配 → 丢弃 ✅
    alias="格林美", text="格林美可持续发展报告"
        → CJK → 无 \\b → re.search("格林美") → 命中 → 保留 ✅
    alias="华友钴业", text="华友钴业ESG审计报告"
        → CJK → 无 \\b → 命中 → 保留 ✅
    alias="Huayou Cobalt", text="huayou cobalt supply chain"
        → 含小写 → IGNORECASE + \\b → 命中 → 保留 ✅

    使用方式
    ────────
        name_zh, name_en = config.get_entity_names(article.company_id)
        if not EntityFilter.passes(article, name_zh, name_en):
            logger.info(f"[过滤] 未包含实体: {article.title}")
            continue
    """

    # 预编译缓存："{alias}|{strict:0/1}|{cjk:0/1}" → compiled pattern
    _PATTERN_CACHE: dict[str, re.Pattern] = {}

    # 检测是否含 CJK（中日韩）Unicode 字符
    _CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

    @classmethod
    def _is_strict_case(cls, alias: str) -> bool:
        """
        是否启用严格大小写匹配。
        规则：alias 全为大写 ASCII 字母且长度 ≤ 5。
        GEM(3) CMOC(4) CATL(4) CNGR(4) RMI(3) → True
        Huayou(6) 格林美(CJK) Ganfeng(混合) → False
        """
        return alias.isupper() and alias.isascii() and len(alias) <= 5

    @classmethod
    def _has_cjk(cls, alias: str) -> bool:
        """检测 alias 是否含有中文/日文/韩文字符。"""
        return bool(cls._CJK_RE.search(alias))

    @classmethod
    def _build_pattern(cls, alias: str) -> re.Pattern:
        """
        为单个 alias 构建（并缓存）正则 pattern，三条分支：

        CJK alias    → re.escape(alias)，无 \\b，IGNORECASE
        严格大小写   → \\b + re.escape(alias) + \\b，无 flag（大小写敏感）
        普通英文     → \\b + re.escape(alias) + \\b，IGNORECASE
        """
        is_cjk    = cls._has_cjk(alias)
        is_strict = (not is_cjk) and cls._is_strict_case(alias)
        cache_key = f"{alias}|{int(is_strict)}|{int(is_cjk)}"

        if cache_key not in cls._PATTERN_CACHE:
            escaped = re.escape(alias)
            if is_cjk:
                # 中文：直接子串匹配，不加 \b
                pattern = re.compile(escaped, re.IGNORECASE)
            elif is_strict:
                # 全大写短缩词：严格大小写 + 单词边界
                pattern = re.compile(r"\b" + escaped + r"\b")
            else:
                # 普通英文混合词：忽略大小写 + 单词边界
                pattern = re.compile(r"\b" + escaped + r"\b", re.IGNORECASE)
            cls._PATTERN_CACHE[cache_key] = pattern

        return cls._PATTERN_CACHE[cache_key]

    @classmethod
    def _match(cls, alias: str, haystack: str) -> bool:
        """对单个 alias 执行 regex 匹配，返回是否命中。"""
        if not alias:
            return False
        return bool(cls._build_pattern(alias).search(haystack))

    @classmethod
    def passes(cls, article: "NewsArticle", name_zh: str, name_en: str) -> bool:
        """
        返回 True 表示文章通过校验（可纳入报告）；False 表示应丢弃。

        检查范围：原始标题（title）+ 深度抓取正文（raw_summary）。
        中英文短名任一命中即通过。

        注意：haystack 保留原始大小写，不做 .lower() 转换——
        严格大小写的 alias 需在原文中搜索；IGNORECASE alias 由 re flag 处理。
        """
        haystack = article.title + " " + article.raw_summary
        return cls._match(name_zh, haystack) or cls._match(name_en, haystack)




class ContentExtractor:
    """
    向新闻原始 URL 发送 GET 请求，提取正文前 200 字纯文本。

    策略（优先级从高到低）：
      1. <article> 标签内容
      2. class/id 含 article|content|post|story|body|main 的容器
      3. 整个 <body> 中的 <p> 标签
    """

    TIMEOUT = 5
    MAX_CHARS = 200
    _SEMANTIC_RE = re.compile(r"article|content|post|story|body|main", re.I)

    @classmethod
    def extract(cls, url: str) -> str:
        """返回纯文本摘要（≤ MAX_CHARS），失败返回空字符串。"""
        real_url = cls._unwrap_redirect(url)
        try:
            resp = requests.get(
                real_url,
                headers=FETCH_HEADERS,
                timeout=cls.TIMEOUT,
                allow_redirects=True,
            )
            if resp.status_code != 200:
                return ""
            return cls._parse_body(resp.text)
        except Exception as exc:
            logger.debug(f"ContentExtractor failed [{real_url[:70]}]: {exc}")
            return ""

    @classmethod
    def _unwrap_redirect(cls, url: str) -> str:
        """解包 Bing apiclick.aspx 等重定向包装，提取真实文章地址。"""
        if "apiclick.aspx" in url:
            qs = parse_qs(urlparse(url).query)
            inner = qs.get("url", [None])[0]
            if inner:
                return unquote(inner)
        return url

    @classmethod
    def _parse_body(cls, html_text: str) -> str:
        soup = BeautifulSoup(html_text, "html.parser")

        # 移除噪音元素
        for noise in soup.find_all(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
            noise.decompose()

        # 定位正文容器
        container = (
            soup.find("article")
            or soup.find(["div", "section", "main"], class_=cls._SEMANTIC_RE)
            or soup.find(["div", "section", "main"], id=cls._SEMANTIC_RE)
            or soup
        )

        return cls._collect_paragraphs(container)

    @classmethod
    def _collect_paragraphs(cls, container) -> str:
        """从容器中收集有意义的段落文本，拼接后截取至 MAX_CHARS。"""
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
        return " ".join(texts)[: cls.MAX_CHARS]


# ─────────────────────────────────────────────────────────
# RSS 抓取器
# ─────────────────────────────────────────────────────────

class NewsFetcher:
    """
    双通道 RSS 抓取：
      - 中文查询  → Bing News RSS（对中文关键词索引效果更好）
      - 其他语言  → Google News RSS（hl/gl 精确定向），失败时回退 Bing
    """

    TIMEOUT = 20
    MAX_RESULTS = 8

    @classmethod
    def fetch(cls, query: str, lang: str) -> list[dict]:
        if lang == "zh":
            results = cls._bing_rss(query)
        else:
            results = cls._google_rss(query, lang)
            if not results:
                results = cls._bing_rss(query)

        # 清洗 description
        for r in results:
            r["description"] = strip_html(r.get("description", ""))
        return results

    # ── Bing ────────────────────────────────────────────

    @classmethod
    def _bing_rss(cls, query: str) -> list[dict]:
        articles: list[dict] = []
        try:
            resp = requests.get(
                "https://www.bing.com/news/search",
                headers=FETCH_HEADERS,
                params={"q": query, "format": "rss", "first": "1"},
                timeout=cls.TIMEOUT,
            )
            resp.raise_for_status()
            for item_xml in re.findall(r"<item>(.*?)</item>", resp.text, re.DOTALL)[: cls.MAX_RESULTS]:
                parsed = cls._parse_item(item_xml, source_tag="News:Source", source_as_attr=False)
                if parsed:
                    articles.append(parsed)
        except Exception as exc:
            logger.debug(f"Bing RSS [{query[:40]}]: {exc}")
        return articles

    # ── Google ──────────────────────────────────────────

    @classmethod
    def _google_rss(cls, query: str, lang: str) -> list[dict]:
        articles: list[dict] = []
        lc = LANG_CONFIG.get(lang, {})
        hl = lc.get("hl", "en")
        gl = lc.get("gl", "US")
        url = (
            f"https://news.google.com/rss/search"
            f"?q={quote(query + ' when:14d')}&hl={hl}&gl={gl}&ceid={gl}:{hl}"
        )
        try:
            resp = requests.get(url, headers=FETCH_HEADERS, timeout=cls.TIMEOUT)
            if resp.status_code != 200:
                return articles
            for item_xml in re.findall(r"<item>(.*?)</item>", resp.text, re.DOTALL)[: cls.MAX_RESULTS]:
                parsed = cls._parse_item(item_xml, source_tag="source", source_as_attr=True)
                if parsed:
                    articles.append(parsed)
        except Exception as exc:
            logger.debug(f"Google RSS [{lang}][{query[:40]}]: {exc}")
        return articles

    # ── 通用 XML 解析 ────────────────────────────────────

    @staticmethod
    def _parse_item(item_xml: str, source_tag: str, source_as_attr: bool) -> Optional[dict]:
        t  = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item_xml)
        l  = re.search(r"<link>(.*?)</link>", item_xml)
        d  = re.search(r"<pubDate>(.*?)</pubDate>", item_xml)
        de = re.search(r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>", item_xml)
        if source_as_attr:
            s = re.search(rf'{source_tag}="([^"]*)"', item_xml)
        else:
            s = re.search(rf"<{source_tag}>(.*?)</{source_tag}>", item_xml)

        title = (t.group(1) if t else "").strip()
        link  = (l.group(1) if l else "").strip()
        if not title or not link:
            return None

        return {
            "title":       title,
            "date":        (d.group(1)  if d  else "").strip(),
            "source":      ((s.group(1) if s  else "Unknown").strip()),
            "url":         link,
            "description": ((de.group(1) if de else "").strip()),
        }


# ─────────────────────────────────────────────────────────
# 翻译引擎
# ─────────────────────────────────────────────────────────

class TranslationEngine:
    """
    使用 deep_translator.GoogleTranslator 将非中文内容翻译为简体中文。
    自动重试 3 次，指数退避。
    """

    MAX_RETRIES = 3
    BASE_DELAY = 2.0

    @classmethod
    def to_chinese(cls, text: str, source_lang: str = "auto") -> str:
        """将任意语言文本翻译为 zh-CN，失败时返回原文。"""
        if not text or not text.strip():
            return text
        # source_lang 来自 LANG_CONFIG[lang]["hl"]，GoogleTranslator 接受 "zh-CN"、"en" 等
        for attempt in range(cls.MAX_RETRIES):
            try:
                result = GoogleTranslator(source=source_lang, target="zh-CN").translate(text)
                if result:
                    return str(result)
            except Exception as exc:
                logger.warning(f"Translation attempt {attempt + 1} failed: {exc}")
                if attempt < cls.MAX_RETRIES - 1:
                    time.sleep(cls.BASE_DELAY * (attempt + 1))
        return text

    @classmethod
    def translate_article(cls, article: NewsArticle) -> None:
        """
        原地翻译文章的标题与摘要。
        中文文章直接复制字段，不调用翻译 API。
        """
        if article.lang == "zh":
            article.translated_title   = article.title
            article.translated_summary = article.raw_summary
            return

        src = LANG_CONFIG.get(article.lang, {}).get("hl", "auto")
        article.translated_title   = cls.to_chinese(article.title,       source_lang=src)
        article.translated_summary = cls.to_chinese(article.raw_summary, source_lang=src)


# ─────────────────────────────────────────────────────────
# Markdown 报告生成器 v5
# ─────────────────────────────────────────────────────────

class MarkdownReportWriter:
    """
    层级结构：
      # 企业全称（H1）
        ## 主题（H2）
          ### [原始标题](url)（H3，每条新闻）
          - 标题翻译（仅非中文）
          - 中文摘要
          - 情报属性
          ---
    """

    def __init__(self, articles: list[NewsArticle], config: Optional[AgentConfig] = None):
        self.articles = articles
        self.config   = config
        self.df       = pd.DataFrame([a.__dict__ for a in articles]) if articles else pd.DataFrame()

    def _display_name(self, company_id: str) -> str:
        if self.config:
            return self.config.get_full_display_name(company_id)
        return company_id

    def generate(self, path: str = "esg_global_report.md") -> None:
        if self.df.empty:
            Path(path).write_text(
                "# 🌍 ESG Global Intelligence Report\n\n> No data collected in the current time window.\n",
                encoding="utf-8",
            )
            logger.info(f"Empty report written to {path}")
            return

        # 统一 parsed_date 为带时区 datetime（便于排序）
        self.df["_sort_dt"] = self.df["parsed_date"].where(
            self.df["parsed_date"].notna(),
            pd.to_datetime(self.df["date"], errors="coerce", utc=True),
        )

        lines = self._build_report()
        Path(path).write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"Report saved: {path} ({len(self.df)} articles)")

    # ── 报告骨架 ─────────────────────────────────────────

    def _build_report(self) -> list[str]:
        now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total  = len(self.df)
        firms  = self.df["company_id"].nunique()
        topics = self.df["topic_zh"].nunique()
        langs  = self.df["lang"].nunique()
        srcs   = self.df["source"].nunique()

        valid_dates = [d for d in self.df["parsed_date"] if pd.notna(d)]
        dmin = min(valid_dates).strftime("%Y-%m-%d") if valid_dates else "?"
        dmax = max(valid_dates).strftime("%Y-%m-%d") if valid_dates else "?"

        lines: list[str] = [
            "# 🌍 ESG 全球情报监控报告",
            "",
            f"> 📅 **生成时间**: {now}",
            f"> 📊 **情报总数**: {total} 条 | 企业: {firms} 家 | 主题: {topics} 类 | 语种: {langs} 种 | 来源: {srcs} 家",
            f"> 📆 **覆盖时段**: {dmin} ~ {dmax}（{self.config.days_limit if self.config else 14} 天窗口）",
            "",
            "---",
            "",
            "## 📑 目录",
            "",
        ]

        # 目录：企业 > 主题
        for cid in sorted(self.df["company_id"].unique()):
            display = self._display_name(cid)
            lines.append(f"- **{display}**")
            sub = self.df[self.df["company_id"] == cid]
            for topic in sorted(sub["topic_zh"].unique()):
                count = len(sub[sub["topic_zh"] == topic])
                anchor = self._anchor(cid, topic)
                lines.append(f"  - [{topic}（{count} 条）](#{anchor})")

        lines += ["", "---", ""]

        # 正文：企业 > 主题 > 新闻
        for cid in sorted(self.df["company_id"].unique()):
            display = self._display_name(cid)
            lines.append(f"# {display}")
            lines.append("")

            sub_company = self.df[self.df["company_id"] == cid]
            for topic in sorted(sub_company["topic_zh"].unique()):
                anchor = self._anchor(cid, topic)
                sub_topic = sub_company[sub_company["topic_zh"] == topic].sort_values(
                    "_sort_dt", ascending=False, na_position="last"
                )
                lines.append(f"## {topic} {{#{anchor}}}")
                lines.append(f"> 共 {len(sub_topic)} 篇相关情报")
                lines.append("")

                for _, row in sub_topic.iterrows():
                    lines.extend(self._render_article(row))

            lines += ["", "---", ""]

        lines += [
            "> 🤖 *本报告由 ESG Intelligence Agent v5 自动生成。*",
            "> 🌐 *多语种内容经 GoogleTranslator 翻译为中文摘要。*",
            "> ⚠️  *数据来源为公开 RSS 新闻源，仅供决策参考，不构成投资或法律建议。*",
        ]
        return lines

    # ── 单条新闻渲染 ─────────────────────────────────────

    @staticmethod
    def _render_article(row: pd.Series) -> list[str]:
        """
        严格按以下模板渲染：

            ### [{原始标题}]({url})
            - **标题翻译**: {中文标题}  ← 仅非中文文章显示
            - **中文摘要**: {摘要}
            - **情报属性**: 🌐 源语言: {lang} | 📰 来源: {source} | 🕒 日期: {date}

            ---
        """
        title  = str(row.get("title", "")).strip()
        url    = str(row.get("url",   "")).strip()
        lang   = str(row.get("lang",  "")).strip()
        source = str(row.get("source", "Unknown"))[:50].strip()
        date_s = fmt_date(row.get("parsed_date") or row.get("date", ""))
        lang_label = LANG_CONFIG.get(lang, {}).get("label", lang)

        parts: list[str] = [f"### [{title}]({url})", ""]

        # 标题翻译（仅非中文）
        if lang != "zh":
            trans_title = str(row.get("translated_title", "")).strip()
            if trans_title and trans_title != title:
                parts.append(f"- **标题翻译**: {trans_title}")

        # 中文摘要
        summary = (
            str(row.get("translated_summary", "")).strip()
            or str(row.get("raw_summary", "")).strip()
            or "_（原文内容抓取受限，请点击标题查看原文）_"
        )
        parts.append(f"- **中文摘要**: {summary[:200]}")

        # 情报属性
        parts.append(
            f"- **情报属性**: 🌐 源语言: {lang_label} | "
            f"📰 来源: {source} | "
            f"🕒 日期: {date_s}"
        )

        parts += ["", "---", ""]
        return parts

    # ── 辅助 ────────────────────────────────────────────

    @staticmethod
    def _anchor(company_id: str, topic: str) -> str:
        """生成 Markdown 锚点（GitHub 兼容：小写+连字符）。"""
        raw = f"{company_id}-{topic}"
        return re.sub(r"[^\w\u4e00-\u9fff-]", "-", raw).lower()


# ─────────────────────────────────────────────────────────
# 智能体主控
# ─────────────────────────────────────────────────────────

class ESGIntelligenceAgent:
    """
    六阶段流水线（v6）：
      Phase 1   — RSS 多语言抓取（高级搜索语法：精确引号 + 负面词）
      Phase 2   — URL 级去重（title 子集去重）
      Phase 3   — 深度正文抓取（ContentExtractor）
      Phase 2.5 — 实体出现校验（EntityFilter，抓取后置过滤）
      Phase 4   — 机器翻译（非中文 → 中文）
      Phase 5   — Markdown 报告生成
    """

    def __init__(self, config_path: str = "config.yaml"):
        self.config       = AgentConfig.from_yaml(config_path)
        self.articles:  list[NewsArticle] = []
        self._seen_urls: set[str]         = set()
        self._cutoff = datetime.now(timezone.utc) - timedelta(days=self.config.days_limit)
        logger.info(
            f"Loaded config: {len(self.config.companies)} companies × "
            f"{len(self.config.topics)} topics × "
            f"{len(self.config.languages)} languages | "
            f"cutoff: {self._cutoff.strftime('%Y-%m-%d')}"
        )

    # ── 钉钉推送 ──────────────────────────────────────────

    def push_to_dingtalk(self, report_path: str = "esg_global_report.md") -> None:
        """将生成的 Markdown 报告推送到钉钉群机器人。"""
        webhook = os.environ.get("DINGTALK_WEBHOOK")
        if not webhook:
            logger.info("未配置钉钉 Webhook (DINGTALK_WEBHOOK)，跳过推送。")
            return

        try:
            with open(report_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 钉钉机器人对单条 Markdown 有长度限制，截断至 15000 字符保底
            if len(content) > 15000:
                content = content[:15000] + "\n\n> ⚠️ 报告过长，已自动截断。完整内容请查看源文件。"

            headers = {"Content-Type": "application/json"}
            data = {
                "msgtype": "markdown",
                "markdown": {
                    "title": "🌍 每日全球 ESG 与供应链合规简报",
                    "text": content,
                },
            }

            logger.info("正在向钉钉发送情报简报...")
            response = requests.post(webhook, headers=headers, data=json.dumps(data))
            logger.info(f"钉钉服务器返回: {response.text}")

        except Exception as exc:
            logger.error(f"钉钉推送失败: {exc}")

    # ── 入口 ─────────────────────────────────────────────

    def run(self) -> None:
        t0 = time.monotonic()
        query_matrix = self.config.build_query_matrix()
        logger.info(f"Query matrix: {len(query_matrix)} tasks")

        # ── Phase 1: RSS Fetch ───────────────────────────
        skipped = 0
        for idx, q in enumerate(query_matrix, 1):
            logger.info(f"[{idx:>4}/{len(query_matrix)}] [{q.lang}] {q.query}")
            for raw in NewsFetcher.fetch(q.query, q.lang):
                parsed_date = parse_rss_date(raw["date"])
                # 过滤时间窗口外的文章（parsed_date 为 None 则保留，避免误删）
                if parsed_date and parsed_date < self._cutoff:
                    skipped += 1
                    continue
                if raw["url"] in self._seen_urls:
                    continue
                self._seen_urls.add(raw["url"])
                self.articles.append(NewsArticle(
                    title       = raw["title"],
                    date        = raw["date"],
                    source      = raw["source"],
                    url         = raw["url"],
                    description = raw["description"],
                    company_id  = q.company_id,
                    topic_zh    = q.topic_zh,
                    lang        = q.lang,
                    parsed_date = parsed_date,
                ))
            time.sleep(1.2)   # 礼貌性抓取间隔

        logger.info(
            f"Phase 1 done. Skipped {skipped} old articles. "
            f"Collected {len(self.articles)} articles."
        )

        if not self.articles:
            MarkdownReportWriter([], self.config).generate()
            return

        # ── Phase 2: Dedup by title ──────────────────────
        raw_count = len(self.articles)
        df_tmp = pd.DataFrame([a.__dict__ for a in self.articles]).drop_duplicates(
            subset=["title"], keep="first"
        )
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
                # 回退：使用 RSS description（若有意义）
                desc = article.description.strip()
                if desc and desc != article.title and len(desc) > 15:
                    article.raw_summary = desc[:200]

            if (idx + 1) % 10 == 0:
                logger.info(f"  Progress: {idx + 1}/{len(self.articles)}")
                time.sleep(0.3)

        logger.info(
            f"Phase 3 done. Body text extracted: "
            f"{extracted_count}/{len(self.articles)}"
        )

        # ── Phase 2.5: Entity Presence Filter ───────────
        # 在正文抓取完成后、翻译开始前执行实体出现校验：
        # 标题或 raw_summary 中必须含有公司中/英文短名，否则丢弃。
        pre_filter_count = len(self.articles)
        filtered_articles: list[NewsArticle] = []
        for article in self.articles:
            name_zh, name_en = self.config.get_entity_names(article.company_id)
            if EntityFilter.passes(article, name_zh, name_en):
                filtered_articles.append(article)
            else:
                logger.info(f"[过滤] 未包含实体: {article.title}")
        self.articles = filtered_articles
        logger.info(
            f"Phase 2.5 entity filter: {pre_filter_count} → {len(self.articles)} "
            f"（丢弃 {pre_filter_count - len(self.articles)} 条无关文章）"
        )

        if not self.articles:
            MarkdownReportWriter([], self.config).generate()
            return

        # ── Phase 4: Translation ─────────────────────────
        non_zh = [a for a in self.articles if a.lang != "zh"]
        if non_zh:
            logger.info(f"Phase 4: Translating {len(non_zh)} non-Chinese articles...")
            for idx, article in enumerate(non_zh):
                TranslationEngine.translate_article(article)
                if (idx + 1) % 5 == 0:
                    logger.info(f"  Translated: {idx + 1}/{len(non_zh)}")
                    time.sleep(0.5)
            logger.info("Phase 4 done.")

        # ── Phase 5: Report ──────────────────────────────
        report_path = "esg_global_report.md"
        MarkdownReportWriter(self.articles, self.config).generate(report_path)

        elapsed = time.monotonic() - t0
        valid_dates = [a.parsed_date for a in self.articles if a.parsed_date]
        dmin = min(valid_dates).strftime("%Y-%m-%d") if valid_dates else "?"
        dmax = max(valid_dates).strftime("%Y-%m-%d") if valid_dates else "?"
        logger.info(
            f"All done in {elapsed:.1f}s | "
            f"{len(self.articles)} articles | "
            f"{extracted_count} with body text | "
            f"Coverage: {dmin} ~ {dmax}"
        )


# ─────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    agent = ESGIntelligenceAgent()
    agent.run()
    agent.push_to_dingtalk()

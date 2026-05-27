#!/usr/bin/env python3
"""
ESG 情报监控智能体 v7 — DeepSeek 大模型语义降噪 + 智能摘要
═══════════════════════════════════════════════════════════════════════════════
架构说明
────────
• AgentConfig.from_yaml()        — 读取多维字典结构的 config.yaml，含 negative_filters
• AgentConfig.build_query_matrix() — 生成带精确引号+负面词的高级搜索查询矩阵
• EntityFilter.passes()          — 实体出现校验：正文/标题必须含公司短名
• NewsFetcher                    — Google RSS / Bing RSS 双通道抓取
• ContentExtractor               — 向原始 URL 深度抓取正文前 200 字
• ESGIntelligenceAgent.process_intelligence_with_llm() — DeepSeek 大模型语义降噪与结构化摘要
• MarkdownReportWriter           — 基于 LLM 输出 JSON 的智能报告
═══════════════════════════════════════════════════════════════════════════════
v7 升级
───────
1. 移除基于规则的机器翻译（deep-translator），全面切入 DeepSeek 大模型
2. 大模型实体消歧：自动排除重名噪音（如 GEM 假肢品牌、法国协会等）
3. 结构化 JSON 输出：公司 / 风险类别 / 中文标题 / 高管洞察 / 来源 / 日期 / URL
4. 报告分类维度从原始布尔搜索词升级为业务视角风险标签
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
# Markdown 报告生成器 v7（基于 LLM 结构化 JSON 输出）
# ─────────────────────────────────────────────────────────

class MarkdownReportWriter:
    """
    层级结构（v7）：
      # 企业全称（H1）
        ## 风险类别（H2，LLM 输出的 risk_category）
          > 洞察摘要（insight）
          📅 日期 | 📰 来源
          🔗 [原文标题](url)
          ---
    """

    def __init__(self, intelligence_data: list[dict], config: Optional[AgentConfig] = None):
        self.data   = intelligence_data or []
        self.config = config
        self.df     = pd.DataFrame(self.data) if self.data else pd.DataFrame()

    def _display_name(self, company_id: str) -> str:
        if self.config:
            return self.config.get_full_display_name(company_id)
        return company_id

    def generate(self, path: str = "esg_global_report.md") -> None:
        if self.df.empty or "company" not in self.df.columns:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            Path(path).write_text(
                f"# 🌍 ESG 全球情报监控报告\n\n"
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

    # ── 报告骨架（v8：按风险主题透视，非按企业分组） ─────

    def _build_report(self) -> list[str]:
        now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total = len(self.df)
        firms = self.df["company"].nunique() if "company" in self.df.columns else 0
        cats  = self.df["risk_category"].nunique() if "risk_category" in self.df.columns else 0

        dates = pd.to_datetime(self.df.get("date", pd.Series(dtype=str)), errors="coerce")
        dmin = dates.min().strftime("%Y-%m-%d") if dates.notna().any() else "?"
        dmax = dates.max().strftime("%Y-%m-%d") if dates.notna().any() else "?"

        lines: list[str] = [
            "# 🌍 ESG 全球情报监控报告",
            "",
            f"> 📅 **生成时间**: {now}",
            f"> 📊 **情报总数**: {total} 条 | 企业: {firms} 家 | 风险类别: {cats} 类",
            f"> 📆 **覆盖时段**: {dmin} ~ {dmax}",
            "",
            "---",
            "",
            "## 📑 目录",
            "",
        ]

        # 目录：风险类别（一级），每个类别下列出涉及的企业及数量
        for cat in sorted(self.df["risk_category"].unique()):
            sub_cat = self.df[self.df["risk_category"] == cat]
            count = len(sub_cat)
            anchor = self._anchor(cat)
            lines.append(f"- **{cat}**（{count} 条）")
            # 列出该类别下的企业分布
            for company in sorted(sub_cat["company"].unique()):
                co_count = len(sub_cat[sub_cat["company"] == company])
                lines.append(f"  - {company}: {co_count} 条")
            lines.append("")

        lines += ["---", ""]

        # 正文：风险类别（H1） > 情报条目
        for cat in sorted(self.df["risk_category"].unique()):
            anchor = self._anchor(cat)
            sub_cat = self.df[self.df["risk_category"] == cat].copy()
            # 按日期降序
            sub_cat["_sort_dt"] = pd.to_datetime(sub_cat.get("date", pd.Series(dtype=str)), errors="coerce")
            sub_cat = sub_cat.sort_values("_sort_dt", ascending=False, na_position="last")

            lines.append(f"# {cat} {{#{anchor}}}")
            lines.append(f"> 共 {len(sub_cat)} 篇情报")
            lines.append("")

            for _, row in sub_cat.iterrows():
                lines.extend(self._render_item(row))

            lines += ["---", ""]

        lines += [
            "> 🤖 *本报告由 ESG Intelligence Agent 驱动，经 DeepSeek 大模型进行实体消歧与智能摘要。*",
            "> ⚠️  *数据来源为公开 RSS 新闻源，仅供决策参考，不构成投资或法律建议。*",
        ]
        return lines

    # ── 单条情报渲染（v8：企业名加粗引导） ───────────────

    @staticmethod
    def _render_item(row: pd.Series) -> list[str]:
        company  = str(row.get("company", "")).strip()
        title_cn = str(row.get("title_cn", "")).strip()
        url      = str(row.get("url", "")).strip()
        insight  = str(row.get("insight", "")).strip()
        source   = str(row.get("source", "Unknown"))[:50].strip()
        date_s   = str(row.get("date", ""))[:10]

        parts: list[str] = []

        # 企业名加粗 + 洞察摘要
        if insight:
            if company:
                parts.append(f"> **{company}** — {insight}")
            else:
                parts.append(f"> {insight}")
        elif company:
            parts.append(f"> **{company}**")
        parts.append("")

        # 元信息
        meta_parts = []
        if date_s:
            meta_parts.append(f"📅 {date_s}")
        if source:
            meta_parts.append(f"📰 {source}")
        parts.append("  ".join(meta_parts))
        parts.append("")

        # 标题 + 链接
        if url and title_cn:
            parts.append(f"🔗 [{title_cn}]({url})")
        elif title_cn:
            parts.append(f"🔗 {title_cn}")
        elif url:
            parts.append(f"🔗 [原文链接]({url})")

        parts += ["", "---", ""]
        return parts

    # ── 辅助 ────────────────────────────────────────────

    @staticmethod
    def _anchor(category: str) -> str:
        """生成 Markdown 锚点（GitHub 兼容：小写+连字符）。"""
        raw = category
        return re.sub(r"[^\w\u4e00-\u9fff-]", "-", raw).lower()


# ─────────────────────────────────────────────────────────
# 智能体主控
# ─────────────────────────────────────────────────────────

class ESGIntelligenceAgent:
    """
    六阶段流水线（v7）：
      Phase 1   — RSS 多语言抓取（高级搜索语法：精确引号 + 负面词）
      Phase 2   — URL 级去重（title 子集去重）
      Phase 3   — 深度正文抓取（ContentExtractor）
      Phase 2.5 — 实体出现校验（EntityFilter，抓取后置过滤）
      Phase 4   — DeepSeek 大模型语义降噪与结构化摘要（process_intelligence_with_llm）
      Phase 5   — Markdown 报告生成（基于 LLM 输出的 JSON）
    """

    # ── DeepSeek API 配置 ────────────────────────────────

    DEEPSEEK_BASE_URL = "https://api.deepseek.com"
    DEEPSEEK_MODEL    = "deepseek-chat"

    @classmethod
    def _build_system_prompt(cls, company_names: list[str]) -> str:
        """构建 DeepSeek System Prompt，极其严厉的实体消歧规则与强制 JSON 输出格式。"""
        companies_str = "\n".join(f"  - {name}" for name in company_names)
        return f"""你是一个顶级的 ESG 供应链风险分析师。你的核心任务是【实体消歧】与【风险提炼】。

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

### 步骤 2：风险提炼
对于判定为真实目标企业的情报：
- 赋予一个**通俗专业的业务分类**（如：劳工权益、环境污染、供应链合规、社区冲突、安全事故、可持续发展、监管政策）。
- **严禁使用任何布尔逻辑词作为分类**！禁止在分类中出现 "OR"、"AND"、"罢工 OR 抗议" 等搜索关键词。
- 如果新闻内容虽提及目标企业但无实质性 ESG 风险信息（如纯股价涨跌、财报数据、产品广告），也请剔除。

### 步骤 3：高管洞察撰写
提炼 ≤50 字的中文高管洞察摘要，必须明确包含：时间、地点、涉事主体、风险定级。
- **叙事风格要求**：采用多样化、简练的商业简报叙事风格。像专业的顶级商业调查分析师一样，一语道破核心事件与风险定级，语言自然、犀利且富有穿透力。
- **坚决避免机械的句式模板**：绝不要每条都以"xxxx年x月，[企业名]..."这类刻板句式开头。请灵活变换表达方式，让每条摘要都具有独立的阅读价值。

### 步骤 4：标题翻译
将非中文原始标题准确翻译为简体中文，填入 title_cn 字段。

## 强制输出格式
请严格返回纯 JSON 数组（不要包含任何 markdown 代码块符号如 ```json），JSON 结构：
[
  {{
    "company": "企业全称（必须精确匹配目标企业列表中某一项）",
    "risk_category": "通俗业务分类（如：劳工权益、环境污染、供应链合规）",
    "title_cn": "准确完整的中文标题",
    "insight": "50字以内的高管洞察摘要（完整句子，时间+地点+主体+风险定级）",
    "source": "来源媒体名称",
    "date": "YYYY-MM-DD 格式日期",
    "url": "原文链接"
  }}
]

如果没有符合条件的有效情报，请返回空数组 []。"""

    @staticmethod
    def _extract_json_from_response(text: str) -> str:
        """从 LLM 响应中提取纯 JSON：移除可能的 markdown 代码块标记。"""
        text = text.strip()
        # 移除 ```json ... ``` 包装
        if text.startswith("```"):
            # 找到第一个换行后的内容
            first_nl = text.find("\n")
            if first_nl != -1:
                text = text[first_nl + 1:]
            # 移除末尾 ```
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
        return text.strip()

    def process_intelligence_with_llm(self, raw_data_list: list[dict]) -> list[dict]:
        """
        将抓取+实体过滤后的原始文章列表发送至 DeepSeek 大模型，
        进行语义降噪、实体消歧、翻译与结构化摘要提取。

        Args:
            raw_data_list: 经过实体过滤后的原始文章数据列表，每个元素包含：
                company_id, title, date, source, url, raw_summary, lang, topic_zh

        Returns:
            LLM 清洗后的结构化情报 JSON 列表
        """
        if not raw_data_list:
            logger.info("No raw data to process with LLM.")
            return []

        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            logger.error("环境变量 DEEPSEEK_API_KEY 未设置，无法调用 DeepSeek 大模型。回退到原始数据处理。")
            return self._fallback_processing(raw_data_list)

        # 构建目标企业列表
        company_names = [
            self.config.get_full_display_name(c["id"]) for c in self.config.companies
        ]

        # 构建用户消息：将每篇文章格式化为结构化文本
        articles_text_parts = []
        for i, item in enumerate(raw_data_list):
            company_display = self.config.get_full_display_name(item.get("company_id", ""))
            lang_label = LANG_CONFIG.get(item.get("lang", ""), {}).get("label", item.get("lang", ""))
            articles_text_parts.append(
                f"--- 文章 #{i+1} ---\n"
                f"原始标题: {item.get('title', '')}\n"
                f"语种: {lang_label}\n"
                f"日期: {fmt_date(item.get('parsed_date') or item.get('date', ''))}\n"
                f"来源: {item.get('source', 'Unknown')}\n"
                f"搜索目标企业: {company_display}\n"
                f"正文摘要: {item.get('raw_summary', '')[:300]}\n"
                f"URL: {item.get('url', '')}"
            )

        user_message = "\n\n".join(articles_text_parts)

        logger.info(f"Sending {len(raw_data_list)} articles to DeepSeek LLM for semantic processing...")

        try:
            client = OpenAI(
                api_key=api_key,
                base_url=self.DEEPSEEK_BASE_URL,
            )

            response = client.chat.completions.create(
                model=self.DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": self._build_system_prompt(company_names)},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.1,  # 低温度保证输出稳定
                max_tokens=4096,
            )

            raw_output = response.choices[0].message.content or ""
            logger.info(f"LLM response received ({len(raw_output)} chars).")

            # 提取 JSON
            json_text = self._extract_json_from_response(raw_output)
            result = json.loads(json_text)

            if not isinstance(result, list):
                logger.warning("LLM returned non-list JSON, wrapping in list.")
                result = [result] if isinstance(result, dict) else []

            logger.info(f"LLM returned {len(result)} structured intelligence items.")
            return result

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM JSON response: {e}")
            logger.debug(f"Raw LLM output (first 500 chars): {raw_output[:500] if 'raw_output' in dir() else 'N/A'}")
            return self._fallback_processing(raw_data_list)
        except Exception as e:
            logger.error(f"LLM processing failed: {e}")
            return self._fallback_processing(raw_data_list)

    def _fallback_processing(self, raw_data_list: list[dict]) -> list[dict]:
        """
        当 DeepSeek API 不可用时的回退处理：将原始数据转换为基本 JSON 结构。
        【关键】绝不使用原始的 topic_zh 作为 risk_category，因为其中包含
        布尔搜索词（如 "罢工 OR 抗议 OR 原住民 OR 劳工 OR 污染 OR 事故"）。
        """
        logger.warning("Using fallback processing (no LLM enhancement).")
        results = []
        for item in raw_data_list:
            # 从 company_id 映射到全称，topic_zh 被丢弃——决不能泄露到报告
            results.append({
                "company": self.config.get_full_display_name(item.get("company_id", "")),
                "risk_category": "潜在风险情报",
                "title_cn": item.get("title", ""),
                "insight": (item.get("raw_summary", "") or item.get("description", ""))[:50],
                "source": item.get("source", "Unknown"),
                "date": fmt_date(item.get("parsed_date") or item.get("date", "")),
                "url": item.get("url", ""),
            })
        return results

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

        # ── Phase 4: DeepSeek LLM Semantic Processing ────
        raw_data_list = []
        for article in self.articles:
            raw_data_list.append({
                "company_id": article.company_id,
                "title": article.title,
                "date": article.date,
                "source": article.source,
                "url": article.url,
                "raw_summary": article.raw_summary,
                "lang": article.lang,
                "topic_zh": article.topic_zh,
                "parsed_date": article.parsed_date,
            })

        intelligence_json = self.process_intelligence_with_llm(raw_data_list)
        logger.info(f"Phase 4 done. LLM returned {len(intelligence_json)} intelligence items.")

        # ── Phase 5: Report ──────────────────────────────
        report_path = "esg_global_report.md"
        MarkdownReportWriter(intelligence_json, self.config).generate(report_path)

        elapsed = time.monotonic() - t0
        date_values = [item.get("date", "") for item in intelligence_json]
        dmin = min(date_values) if date_values else "?"
        dmax = max(date_values) if date_values else "?"
        logger.info(
            f"All done in {elapsed:.1f}s | "
            f"{len(intelligence_json)} intelligence items | "
            f"Coverage: {dmin} ~ {dmax}"
        )

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


# ─────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    agent = ESGIntelligenceAgent()
    agent.run()
    agent.push_to_dingtalk()

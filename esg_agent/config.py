"""
esg_agent.config — 数据模型与配置管理。
═══════════════════════════════════════════════════════════════════════════════
通过声明式配置驱动 ESG 多轨道供料引擎。
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import yaml

# ── 模块日志 ──────────────────────────────────────────────
logger = logging.getLogger("esg_agent")  # 复用根 logger

# ── 常量 ─────────────────────────────────────────────────

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


# ── 数据模型 ─────────────────────────────────────────────

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
    practice_companies: list[dict] = field(default_factory=list)
    practice_topics: list[dict] = field(default_factory=list)
    days_limit: int = 14
    query_exclusions_risk: str = ""  # risk 模式排除词
    query_exclusions_practice: str = ""  # practice 模式排除词

    # ── 工厂方法 ──────────────────────────────────────────

    @classmethod
    def from_yaml(cls, path: str = None) -> "AgentConfig":
        if path is None:
            path = str(Path(__file__).parent.parent / "config.yaml")
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))

        # ── Pydantic 配置验证（早期捕获错误） ────────
        try:
            from esg_agent.validators import validate_config
            validate_config(path)
            logger.debug("config.yaml validated successfully")
        except ImportError:
            pass  # Pydantic not available, skip validation
        except Exception as e:
            logger.warning(f"config.yaml validation warning: {e}")

        tracks = raw.get("intelligence_tracks", {})
        topics = raw.get("topics", {})
        return cls(
            companies=raw.get("companies", []),
            geographical_tracks=tracks.get("geographical_tracks", []),
            premium_company_tracks=tracks.get("premium_company_tracks", []),
            premium_global_tracks=tracks.get("premium_global_tracks", []),
            daily_topics=topics.get("daily", []),
            weekly_topics=topics.get("weekly", []),
            practice_companies=raw.get("practice_companies", []),
            practice_topics=raw.get("practice_topics", []),
            days_limit=raw.get("days_limit", 14),
            query_exclusions_risk=str(raw.get("query_exclusions_risk", "")),
            query_exclusions_practice=str(raw.get("query_exclusions_practice", "")),
        )

    # ── 企业显示名 ──────────────────────────────────────

    def get_company_display_name(self, company: dict) -> str:
        zh = company.get("name_zh", "")
        en = company.get("name_en", "")
        return f"{zh} | {en}" if zh and en else (zh or en)

    def get_all_company_display_names(self) -> list[str]:
        return [self.get_company_display_name(c) for c in self.companies]

    def get_practice_company_display_names(self) -> list[str]:
        return [self.get_company_display_name(c) for c in self.practice_companies]

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

    def build_query_tasks(self, mode: str = "daily") -> list[QueryItem]:
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

        mode=practice:
          - 仅轨道 1（地缘通用轨）：practice 企业 × 每语种 × practice 主题关键词
          - 跳过轨道 2（BHRRC）与轨道 3（EFRAG）——这两者都是风险专属
        """
        items: list[QueryItem] = []

        # practice 轨道：独立的企业名单与主题矩阵，只走地理新闻轨
        if mode == "practice":
            companies = self.practice_companies or self.companies
            active_topics = self.practice_topics or self.daily_topics
            logger.info(
                f"Building query tasks for mode=practice: "
                f"{len(active_topics)} practice topics, {len(companies)} practice companies, "
                f"{len(self.geographical_tracks)} geo tracks"
            )
            for company in companies:
                name_zh = company.get("name_zh", "")
                name_en = company.get("name_en", "")
                for geo in self.geographical_tracks:
                    lang = geo.get("lang", "en-US")
                    url_template = geo.get("url_template", "")
                    lang_label = geo.get("lang_label", lang)
                    if not url_template:
                        continue
                    search_term = name_zh if lang in _GEO_CN_LANGS else name_en
                    if not search_term:
                        continue
                    for topic in active_topics:
                        category = topic.get("category", "")
                        keywords = self._get_keywords_for_lang(topic, lang)
                        if not keywords:
                            continue
                        kw_query = self._build_keyword_query(keywords)
                        excl = f" {self.query_exclusions_practice}" if self.query_exclusions_practice else ""
                        full_query = f'"{search_term}" {kw_query} when:{self.days_limit}d{excl}'
                        query_encoded = quote(full_query, safe='/-:()"')
                        final_url = url_template.replace("{query}", query_encoded)
                        items.append(QueryItem(
                            url=final_url,
                            company_name_zh=name_zh,
                            company_name_en=name_en,
                            track_label=f"实践供料 ({lang_label})",
                            lang=lang,
                            topic_category=category,
                        ))
            logger.info(f"Total query tasks generated: {len(items)}")
            return items

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
                    # 组装完整查询: "公司名" (关键词1 OR 关键词2 ...) when:Nd -股票 -产品
                    excl = f" {self.query_exclusions_risk}" if self.query_exclusions_risk else ""
                    full_query = f'"{search_term}" {kw_query} when:{self.days_limit}d{excl}'
                    query_encoded = quote(full_query, safe='/-:()"')
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

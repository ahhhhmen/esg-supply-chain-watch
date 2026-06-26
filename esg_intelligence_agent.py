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
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime as email_parse_date
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from notion_client import Client as NotionClient

from sourcing_engine import SourcingEngine
from backend.utils.references import clean_title, normalize_url, extract_domain_name
from notion_upsert import upsert_notion_page

# ── 从 esg_agent 模块导入已迁移的类和工具 ──────────────────
from esg_agent.config import (
    AgentConfig, QueryItem, NewsArticle,
    FETCH_HEADERS, DINGTALK_WEBHOOK_URL,
    _GEO_CN_LANGS,
)
from esg_agent.fetchers import (
    ContentExtractor, NewsFetcher, resolve_news_url, strip_html,
)
from esg_agent.filters import EntityFilter
from esg_agent.deduplication import JaccardMerger, LLMGlobalConvergence
from esg_agent.reporters import MarkdownReportWriter
from esg_agent.pdf_writer import convert_markdown_to_pdf
from esg_agent.scorer import create_scorer, TavilyScorer
from radar_infra.llm import create_provider, create_cheap_provider, BaseLLMProvider, TokenUsage
from radar_infra.llm import create_llm_retry_decorator

# ─────────────────────────────────────────────────────────
# 日志
# ─────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("esg_agent")

# 常量已迁移至 esg_agent.config 模块，此处通过 import 引用

# Utility functions & classes below have been migrated to:
#   esg_agent.fetchers  (strip_html, ContentExtractor, NewsFetcher, resolve_news_url)
#   esg_agent.filters   (EntityFilter)
#   esg_agent.reporters (MarkdownReportWriter)

# ═══ RETAINED (used by ESGIntelligenceAgent) ═══

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

    # LLM 供应商（实例级，在 __init__ 中初始化）
    _llm: Optional["BaseLLMProvider"] = None
    _cheap_llm: Optional["BaseLLMProvider"] = None  # 低成本模型（格式化/去重）
    _tavily_scorer: Optional["TavilyScorer"] = None  # Tavily 相关性评分
    _llm_usage: "TokenUsage" = None  # type: ignore[assignment]

    # ── 钉钉推送格式化常量 ──────────────────────────────────
    CATEGORY_EMOJI_MAP: dict[str, str] = {
        "早期合规预警": "🔍",
        "供应链断裂预警": "🔗",
        "政策与市场准入": "🚫",
        "合规与运营危机": "⚠️",
        "机构与声誉预警": "📢",
    }
    CATEGORY_ORDER: list[str] = [
        "供应链断裂预警",
        "政策与市场准入",
        "合规与运营危机",
        "早期合规预警",
        "机构与声誉预警",
    ]
    CATEGORY_DESCRIPTIONS: dict[str, str] = {
        "早期合规预警": "尚未引发断供或停产，但面临官方环保调查、劳工审查或严重违规指控的早期阻力",
        "供应链断裂预警": "仅限物理层面的供给中断（工厂停产、物流瘫痪、核心供应商断供）",
        "政策与市场准入": "仅限引发无法卖货/买货的事件（实体清单、关税惩罚、进出口禁令、强迫劳动扣留）",
        "合规与运营危机": "劳工罢工、重大安全事故、产品召回、严重环保罚单引发的即期运营阻断",
        "机构与声誉预警": "NGO指控、人权机构质询、评级下调等尚未演变为实质停产的高声誉风险事件",
    }

    # ── LLM 调用辅助 ──────────────────────────────────────

    def _call_llm(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.1,
        max_tokens: int = 8192,
        response_format: Optional[dict] = None,
    ) -> Optional[str]:
        """统一 LLM 调用入口，含 tenacity 指数退避重试。"""
        if self._llm is None:
            try:
                self._llm = create_provider()
                logger.info(f"LLM provider initialized: {self._llm.name}")
            except RuntimeError as e:
                logger.error(f"LLM provider init failed: {e}")
                return None

        @create_llm_retry_decorator(max_attempts=3)
        def _do_call():
            return self._llm.complete(
                system_prompt, user_message, temperature, max_tokens, response_format,
            )

        result = _do_call()
        if result is None:
            return None
        content, usage = result
        self._llm_usage = self._llm_usage + usage
        return content

    def _call_llm_cheap(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        response_format: Optional[dict] = None,
    ) -> Optional[str]:
        """低成本 LLM 调用（用于格式化/去重等非关键任务），含重试。"""
        if self._cheap_llm is None:
            try:
                self._cheap_llm = create_cheap_provider()
                logger.info(f"Cheap LLM provider initialized: {self._cheap_llm.name}")
            except RuntimeError as e:
                logger.warning(f"Cheap LLM init failed: {e}, falling back to primary")
                self._cheap_llm = self._llm  # 回退到主模型
        if self._cheap_llm is None:
            return None

        @create_llm_retry_decorator(max_attempts=2)
        def _do_call():
            return self._cheap_llm.complete(
                system_prompt, user_message, temperature, max_tokens, response_format,
            )

        result = _do_call()
        if result is None:
            return None
        content, usage = result
        self._llm_usage = self._llm_usage + usage
        return content

    def _log_token_summary(self) -> str:
        summary = f"💰 Token 消耗: {self._llm_usage.summary()}"
        logger.info(summary)
        return summary

    def _reset_token_stats(self) -> None:
        self._llm_usage = TokenUsage()

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

2. 降噪判定 (is_valid_risk) — 宁可保留早期信号，不可漏过潜在风险：
   - 若实体错误（如"福特省长"、"福特医院"当成福特汽车），值为 false。
   - 若为纯正面事件（如减碳获奖、ESG评级提升、获RFI认证、引入机器人提升效率）、正常商业投资扩张、新产品发布，值为 false。
   - 【早期预警信号 — 必须判定为 true】以下情形即使尚未引发物理停产或制裁，也必须判定为 true：
     · 属地政府/警方/环保部门对该企业发起的正式合规调查、Probe、Investigation。
     · 社区/居民/NGO 对该企业项目的公开抗议、反对或法律诉讼。
     · 工会与企业之间的劳资谈判、罢工投票、集体协商僵局。
     · 企业涉及的产品安全调查、召回程序启动（即便尚未公布具体缺陷）。
     · 监管机构（SEC/FTC/EU/NHTSA/CBP）对该企业的新增审查、质询或警告。
     · 涉及该企业供应链节点的出口管制、关税调整、实体清单变更。
   - 以下可判定为 false：纯股价涨跌、财报亏损、CEO言论、基金调研、券商研报、M&A传闻（未证实）、产品参数披露、技术路线探讨。
   - 【灰色地带判例】若不确定，倾向于判定为 true 并让下游 Python 管道做最终裁决。
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

    @classmethod
    def _build_practice_system_prompt(cls, company_names: list[str], mode: str) -> str:
        """构建 Practice System Prompt — 同业良好实践提取引擎。"""
        companies_str = "\n".join(f"  - {name}" for name in company_names)
        _ = mode
        return f"""# Role: ESG 良好实践提取引擎

# Objective
你是一个结构化数据提取引擎，专注于从新闻流中识别和提取【可复制的行业良好实践】。你的输出将用于同业对标学习，而非风险监控。

# Target Entities
重点关注以下企业及其关联实体：
{companies_str}

# Execution Logic (严格执行)

1. 事件聚类（最高优先级强制规则）：
   阅读所有新闻，将描述【同一核心事件】的多条新闻强制合并为一个独立事件。判断标准：
   - 同一企业在同一时间发生的同一类实践（如"特斯拉发布循环经济报告""宝马取得碳中认证"）不论有多少家媒体以不同标题报道，都必须合并为一条 event。
   - 合并后的 event 必须聚合所有相关来源的媒体名称和对应 URL，放入 sources 数组。
   - 聚合后，必须产出一条最优的 core_event_title_en（标准英文摘要，用于去重）和一条精炼的 display_title_zh（中文标题，用于高管阅读）。
   - 严禁将同一事件的多个媒体报道拆分为多条独立 event。

	2. 有效性判定 (is_valid_practice)：
	   - 若实体错误（如"比亚迪医院""丰田医院"），值为 false。
	   - 若事件为【纯负面/风险类】（罢工、污染、召回、制裁、罚款），值为 false。
	   - 若事件为【营销/品牌宣传】（如发布广告、赞助活动、老板言论、品牌换新），值为 false。
	   - 若事件为【产品发布/展会展示】（如"在某某展会发布""亮相某某博览会""推出新款""概念展示"），且无以下实质证据之一，值为 false：
	     · 量产启动（注明日期或产能数据）
	     · 第三方认证/测试机构出具的性能验证报告
	     · 具体效率/密度/成本数字超出现有行业水平
	     · 已获专利授权（非仅是"申请中"或"布局中"）
	     核心判据：展会展示/产品发布 ≠ 可借鉴实践。如果新闻只是"某公司在某场合展示了某产品"，无论产品参数多漂亮，只要没有量产、认证或专利授权等硬证据，is_valid_practice = false。
	   - 若事件为【纯技术专利申请】（如"申请了专利""专利布局中""专利公开号""专利进入实审"），而非已授权专利，值为 false。专利申请 ≠ 已验证的技术成果。只有明确获得授权的专利（含专利号或授权公告）才可作为有效证据。
	   - 若事件为【股权投资/参股入股】（如"入股""参股""投资""收购股权""战略投资"某公司），值为 false。财务投资不是可复制的运营实践，即使投资对象属于绿色/低碳领域。
	   - 若事件为【股价波动/财报数据】，值为 false。
	   - 只有同时满足"事件类别匹配 + 包含具体成果证据"时，值才为 true。以下是各分类的准入门槛：
	     【机制创新级别——准入门槛最高】
	     · 行业首家通过某权威认证（如首家通过 IRMA 50 标准）
	     · 发布可复现的方法论或开源标准（如零碳工厂建设标准白皮书、供应链尽责管理手册）
	     · 建立行业首个某类治理机制（如首个董事会可持续发展委员会、首个供应链碳排放监测平台）
	     【成果落地级别——准入门槛中等】
	     · 绿色制造：碳中和认证、绿电切换、零碳工厂投产、碳足迹显著下降
	     · 供应链合规：通过 RMI/RBA/IRMA 等认证、发布冲突矿产报告、供应商行为准则升级
	     · 循环经济：电池回收产线投产、闭环回收率达 xx%、梯次利用项目落地
	     · ESG 治理：CDP A-List 入选、MSCI 评级提升、发布首份 ESG 报告
	     · 技术创新：**已授权**专利（非申请中）、无钴/固态/钠离子电池量产、新材料通过验证

	3. 实践分类 (practice_category) — 5 类标准化分类及边界规则：

	   【分类边界铁律——必须严格遵守】
	   - 涉及"专利授权""专利获得""技术突破""量产""新材料""电池技术" → 必须归类为 "技术创新与工艺升级"，不可误判为 ESG披露。
	   - 涉及"ESG报告""CDP评级""MSCI评级""董事会委员会""可持续发展报告" → 才归类为 "ESG披露与治理"。
	   - 涉及"入股""参股""投资""收购" → 本身即 is_valid_practice=false，无需纠结分类。

	   各分类定义：
	   - "绿色制造与减碳"：碳中和认证、绿电切换、零碳工厂、清洁生产、可再生能源使用
	   - "供应链尽职调查与合规标杆"：负责任采购、冲突矿产报告、供应链透明度提升、通过 RMI/RBA 认证
	   - "循环经济与回收"：电池回收、闭环回收、梯次利用、再生材料使用、材料回收率提升
	   - "ESG披露与治理"：ESG 报告发布、CDP 评级、MSCI 评级提升、治理结构优化
	   - "技术创新与工艺升级"：无钴电池、固态电池、钠离子电池、新材料、研发突破、**专利授权**

4. 可借鉴度判定 (is_replicable) — 华友钴业视角：
   你需要站在华友钴业（主营前驱体/正极材料/镍钴锂资源）的立场，判定该实践是否可以被华友借鉴或学习。

   【判定为 true 的条件】：
   · 实践经验来自同行业（矿业/电池材料/新能源汽车）且华友具备类似业务条件
   · 技术路径可迁移（如回收工艺、绿电方案、认证路径）
   · 管理最佳实践可跨企业复制（如 ESG 治理架构、CDP 披露经验）

   【判定为 false 的条件】：
   · 纯粹品牌/消费品营销（Apple 零售店碳中和认证 → false, 不适用于矿业）
   · 与华友业务完全无关的领域（互联网公司数据中心效率、金融业绿色债券）
   · 规模/资源要求远超华友当前能力且无中间路径

# Learning Insight 生成规则（华友钴业学习视角）
1. 身份锚定：华友钴业是全球领先的新能源锂电上游材料供应商，主营前驱体与正极材料。所有 insight 必须从"华友可借鉴什么"的视角出发。
2. 结构铁律：每条 insight 必须遵循【客观事实 + 华友可借鉴要点】两段式结构。客观事实描述被提取企业的具体做法；可借鉴要点指出华友如何参考这一实践来优化自身业务。
3. 具体切入点（至少覆盖以下一个维度）：
   - 工艺对标：对方的生产工艺/技术路径是否可被华友的冶炼/前驱体/正极材料产线借鉴
   - 认证路径：对方获取 RMI/RBA/IRMA/CDP 等认证的方法论是否可为华友提供模板
   - 绿电路径：对方的绿电采购策略（PPA/自发自用/绿证）是否适用华友的冶炼基地
   - 回收体系：对方的电池回收/闭环体系设计是否可被华友参考
   - 披露体系：对方的 ESG 报告框架、数据收集方法是否可被华友借鉴
	4. 字数红线：25-80 汉字。低于 25 或超过 80 视为违规。
	   ⚠️ 绝不可为空：learning_insight 字段在任何情况下（包括 is_valid_practice=true 和 false）都不可为空字符串 ""。至少输出一句 25 字以上的客观分析。is_valid_practice=false 时可简写为"纯产品发布，无量产/认证/专利证据"或"股权投资，非运营实践"。
	5. 禁止行为建议：严禁使用"建议""应当""需要""必须"等指导性措辞。仅做"可借鉴"的客观分析。
	6. 正例：
	   - "BYD 通过屋顶光伏+PPA组合实现全球工厂100%绿电。华友在衢州/广西冶炼基地可参考此双轨绿电模型，结合屋顶光伏自用与长期绿电采购协议，降低前驱体碳足迹。"
	   - "Umicore 完成 Hoboken 冶炼厂 IRMA 认证，成为欧洲首家获此认证的钴精炼厂。华友在印尼镍项目可参考其认证路径，优先在火法冶炼环节推动 IRMA 预审。"
	   - "CATL 获得两项电池安全设计中国专利授权，专利覆盖热失控防护与结构设计。华友前驱体/正极材料研发团队可借鉴其安全设计思路，联合电池客户开展正极-电芯协同安全专利布局。"
	7. 反例（违规，绝不可输出）：
	   - "建议华友加强 ESG 披露" — 包含行为建议
	   - "该实践值得关注" — 空洞无物
	   - "" — 空字符串，最严重违规

# Output Format
你必须仅输出合法的 JSON 数据。JSON 结构必须如下：
{{
  "events": [
    {{
      "entity": "企业全称（必须精确匹配目标企业列表中某一项）",
      "core_event_title_en": "统一转换为标准英文的核心事件简短摘要（5-8个词），专门用于 Python 侧去重",
      "display_title_zh": "精炼、专业的纯中文新闻标题，供高管最终阅读",
      "original_language": "识别原始新闻的语种，如 '印尼语', '英语', '德语', '中文'",
	      "learning_insight": "客观事实 + 华友可借鉴要点，25-80字，绝不可为空",
	      "date": "最新日期 YYYY-MM-DD",
	      "sources": [{{"name": "媒体A", "url": "https://example.com/articleA"}}],
	      "practice_category": "上述五大分类之一",
	      "is_valid_practice": true,
	      "is_replicable": true
	    }},
	    {{
	      "entity": "宁德时代",
	      "core_event_title_en": "CATL obtains two battery safety design patents",
	      "display_title_zh": "宁德时代获得两项电池安全设计专利",
	      "original_language": "中文",
	      "learning_insight": "CATL获两项电池安全设计专利授权，覆盖热失控防护与结构创新。华友前驱体/正极材料研发团队可借鉴其安全设计思路，联合电池客户开展材料-电芯协同安全专利布局。",
	      "date": "2026-06-23",
	      "sources": [{{"name": "国家知识产权局", "url": "https://cnipa.gov.cn/example"}}],
	      "practice_category": "技术创新与工艺升级",
	      "is_valid_practice": true,
	      "is_replicable": true
	    }},
	    {{
	      "entity": "宁德时代",
	      "core_event_title_en": "CATL launches Tener sodium-ion storage system at expo",
	      "display_title_zh": "宁德时代在链博会发布天恒钠电储能系统",
	      "original_language": "中文",
	      "learning_insight": "链博会展出产品，无量产时间表、第三方认证或已授权专利证据，属展会宣讲而非可验证的技术突破。",
	      "date": "2026-06-23",
	      "sources": [{{"name": "36氪", "url": "https://36kr.com/example"}}],
	      "practice_category": "技术创新与工艺升级",
	      "is_valid_practice": false,
	      "is_replicable": false
	    }},
	    {{
	      "entity": "宁德时代",
	      "core_event_title_en": "CATL invests in carbon technology startup",
	      "display_title_zh": "宁德时代、阳光电源入股碳科技企业碳生万物",
	      "original_language": "中文",
	      "learning_insight": "股权投资行为，非可复制的运营实践，即使投资对象属绿色低碳领域。",
	      "date": "2026-06-23",
	      "sources": [{{"name": "企查查", "url": "https://qcc.com/example"}}],
	      "practice_category": "ESG披露与治理",
	      "is_valid_practice": false,
	      "is_replicable": false
	    }}
  ]
}}

重要：每条 event 必须同时包含 core_event_title_en、display_title_zh 和 original_language 三个字段，缺一不可。
is_valid_practice 为 false 的条目也必须输出，以便审计追踪。
如果没有收到任何新闻，请返回 {{ "events": [] }}。"""

    # ── Practice 分类常量 ──────────────────────────────────

    PRACTICE_CATEGORY_EMOJI_MAP: dict[str, str] = {
        "绿色制造与减碳": "🌿",
        "供应链尽职调查与合规标杆": "✅",
        "循环经济与回收": "♻️",
        "ESG披露与治理": "📊",
        "技术创新与工艺升级": "💡",
    }
    PRACTICE_CATEGORY_ORDER: list[str] = [
        "技术创新与工艺升级",
        "绿色制造与减碳",
        "循环经济与回收",
        "供应链尽职调查与合规标杆",
        "ESG披露与治理",
    ]
    PRACTICE_CATEGORY_DESCRIPTIONS: dict[str, str] = {
        "绿色制造与减碳": "碳中和认证、绿电切换、零碳工厂达到的具体成就",
        "供应链尽职调查与合规标杆": "通过 RMI/RBA/IRMA 认证、冲突矿产报告、供应链透明度提升的可复制做法",
        "循环经济与回收": "电池回收产线投产、闭环回收、梯次利用、再生材料应用实践",
        "ESG披露与治理": "首份 ESG 报告发布、CDP A-List、MSCI 评级提升、治理架构优化的具体路径",
        "技术创新与工艺升级": "无钴/固态/钠离子电池量产、新型正极材料突破、回收工艺专利",
    }

    # 分批处理常量：每批最多发送的文章数
    BATCH_SIZE = 15

    def _generate_ai_discovery_queries(self, mode: str, company_names: list[str]) -> list[str]:
        """Phase 0.5: 让 AI 生成当日动态搜索词，填补静态关键词矩阵盲区。

        向 DeepSeek 发送专用 prompt，基于当前监控目标、风险类别和历史漏报教训，
        生成 5-10 条 Google News 搜索查询，捕获静态矩阵可能遗漏的新兴威胁。

        Returns:
            搜索查询字符串列表（未编码的原始查询）。
            失败时返回空列表，不阻断主流程。
        """

        companies_str = "\n".join(f"  - {name}" for name in company_names)
        mode_label = "daily（劳工权益）" if mode == "daily" else "weekly（覆盖全部 6 类风险主题）"

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
            raw = self._call_llm_cheap(
                system_prompt, user_message,
                temperature=0.3, max_tokens=1024,
                response_format={"type": "json_object"},
            )
            if raw is None:
                return []

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

    def _weekly_threat_landscape_review(
        self, events: list[dict], report_path: str, token_summary: str
    ) -> None:
        """Phase 6 (weekly only): LLM 分析本周监控盲区，追加到周报末尾。

        将本周所有捕获事件发送给 DeepSeek，分析：
        1. 哪些实体/关键词/区域在当前监控矩阵中缺失
        2. 哪些新兴威胁模式未被覆盖
        3. 对 esg_sources.yaml 和 config.yaml 的具体补充建议

        输出追加到周报文件末尾作为「🔍 监控矩阵盲区分析」章节。
        """

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
            analysis = self._call_llm_cheap(
                system_prompt, user_message,
                temperature=0.2, max_tokens=2048,
            )
            if analysis is None:
                return None

            # 追加到周报文件
            section = f"\n\n---\n\n## 🔍 监控矩阵盲区分析\n\n{analysis}\n"
            with open(report_path, "a", encoding="utf-8") as f:
                f.write(section)
            logger.info(f"[周度审查] 盲区分析已追加到 {report_path}")
            return analysis

        except Exception as e:
            logger.warning(f"[周度审查] LLM 调用失败 ({type(e).__name__}: {e})，跳过态势审查")
            return None

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
        except json.JSONDecodeError:
            # 自动修复常见 LLM JSON 错误
            repaired = cls._repair_llm_json(candidate)
            if repaired is None:
                return None
            try:
                parsed = json.loads(repaired)
            except json.JSONDecodeError as e2:
                logger.warning(f"_extract_events_object: JSON parse failed after repair: {e2}")
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

    @staticmethod
    def _repair_llm_json(text: str) -> Optional[str]:
        """尝试修复 LLM 产出的常见 JSON 格式错误。

        处理策略（按优先级）：
        1. 删除尾部多余的右花括号或方括号（LLM 常多输出一个 `]}` 或 `}}`）
        2. 修复对象/数组中的尾随逗号：`{"a": 1,}` → `{"a": 1}`
        3. 替换智能引号：" " → " "
        4. 补齐缺失的闭合括号（依据开闭括号计数）

        Returns:
            修复后的 JSON 字符串，无法修复则返回 None。
        """
        if not text:
            return None

        t = text.strip()

        # 策略 1: 删除尾部多余闭合符号
        # 常见模式：`}]}` → `}]` 或 `}}}` → `}}`
        import re as _re

        while len(t) > 2 and t[-1] in "}]":
            trial = t[:-1]
            try:
                json.loads(trial)
                return trial
            except json.JSONDecodeError:
                t = trial

        # 策略 2: 修复尾随逗号（在 } 或 ] 前）
        # 匹配 }, ] 模式
        t = _re.sub(r",(\s*[}\]])", r"\1", t)

        # 策略 3: 替换智能引号
        t = t.replace("\u201c", '"').replace("\u201d", '"')  # " "
        t = t.replace("\u2018", "'").replace("\u2019", "'")  # ' '

        # 策略 4: 补齐缺失闭合括号
        # 统计每种括号的开闭数量差
        open_count = t.count("{") - t.count("}")
        close_count = t.count("[") - t.count("]")
        if open_count > 0 and close_count > 0:
            # 先关对象再关数组
            t += "}" * open_count
            t += "]" * close_count
        elif open_count > 0:
            t += "}" * open_count
        elif close_count > 0:
            t += "]" * close_count

        try:
            json.loads(t)
            return t
        except json.JSONDecodeError:
            return None

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
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if mode == "weekly":
                title = "🏛️ ESG 全球地缘与合规周报 (Weekly Strategy Insight)"
                first_line = "🔮【宏观合规战略】全球地缘与准入壁垒周报"
            else:
                title = "🏛️ ESG 全球供应链动态日报 (Daily Intelligence)"
                first_line = "全球供应链合规与风险速递"

            # 汇总今日降噪事件，让报告有内容可读
            noise_lines = []
            for e in all_events[:8]:
                entity = e.get("entity", "?") if isinstance(e, dict) else "?"
                event_title = ""
                if isinstance(e, dict):
                    event_title = str(e.get("display_title_zh") or e.get("core_event_title_en") or e.get("core_event_title", ""))[:80]
                if event_title:
                    noise_lines.append(f"- **{entity}** {event_title}")
            noise_section = ""
            if noise_lines:
                noise_section = "\n".join([
                    "",
                    "## 🔍 今日监测信号（未达风险阈值，已滤除）",
                    "",
                    f"系统共扫描 {len(all_events)} 条语义信号，以下为未命中合规红线的常规动态：",
                    "",
                    *noise_lines,
                    "",
                    "> 以上信号经 Python 确定性降噪流水线判定为非实质性材料冲击，已自动滤除。",
                    "",
                ])

            placeholder = "\n".join([
                f"# {title}",
                "",
                f"> {first_line}",
                f"> 📅 **生成时间**: {now_str}",
                f"> 📊 **情报总数**: 0 条 | 涉及企业: {len(set(e.get('entity', '') for e in all_events if isinstance(e, dict)))} 家",
                f"> 📰 **扫描样本**: {len(all_events)} 篇语义信号 | 均未命中合规红线",
                "",
                "---",
                "",
                "## 📑 今日无风险事件",
                "",
                "今日无新增实质性供应链断裂与合规风险。",
                f"系统今日已成功巡检，分析样本 {len(all_events)} 篇，均未命中合规红线。",
                noise_section,
                "---",
                "",
                "🤖 *本报告由 ESG Intelligence Agent 自动生成，数据来源于公开新闻源。*",
                "⚠️  *仅供决策参考，不构成投资或法律建议。*",
            ])
            Path(report_path).write_text(placeholder, encoding="utf-8")
            logger.info(f"静默阻断占位报告已写入: {report_path}")
            return [], []

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
            dmax = max(all_dates)
        else:
            dmax = datetime.now().strftime("%Y-%m-%d")
        # 周报模式：start_date 严格 = end_date - 7d，确保表头显示 7 天跨度
        if mode == "weekly":
            end_dt = datetime.strptime(dmax, "%Y-%m-%d") if dmax != "?" else datetime.now()
            dmin = (end_dt - timedelta(days=7)).strftime("%Y-%m-%d")
        else:
            dmin = min(all_dates) if all_dates else dmax

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
        return compatible, valid_events

    @classmethod
    def _generate_practice_report_and_filter(
        cls, all_events: list[dict], mode: str, report_path: str = "esg_practice_report.md",
    ) -> tuple[list[dict], list[dict]]:
        """Practice 轨道确定性渲染：过滤 → 语义合并 → 分组 → 生成 Markdown 报告。

        与 _generate_v10_report_and_filter 的差异：
          - 用 is_valid_practice 替代 is_valid_risk 过滤
          - is_replicable 作标签展示（不过滤）
          - 用 practice_category 分组（实践 5 类）
          - 报告标题和洞察视角为同业实践学习
        """
        # ── 1. 确定性降噪（Python 物理隔绝） ──
        invalid_events: list[dict] = []
        valid_events: list[dict] = []
        for event in all_events:
            if not isinstance(event, dict):
                continue
            if event.get("is_valid_practice") is False:
                invalid_events.append(event)
            else:
                valid_events.append(event)

        for e in invalid_events:
            title_key = e.get("core_event_title_en") or "?"
            logger.info(f"[practice 降噪] 已过滤(无效实践): {e.get('entity', '?')} | {str(title_key)[:60]}")
        logger.info(
            f"Practice 降噪: {len(invalid_events)} invalid -> dropped, {len(valid_events)} valid -> report"
        )

        # ── 1.5. 洞察熔断（Python 物理隔绝，宁缺毋滥） ──
        # 即使 is_valid_practice=true，若 LLM 未生成有效洞察，仍予废弃
        before_melt = len(valid_events)
        quality_events: list[dict] = []
        for event in valid_events:
            insight = str(event.get("learning_insight", "")).strip()
            # 空字符串 / 占位符 / 过短 / 仅为格式模板 → 直接剔除
            if (
                not insight
                or "待进一步分析" in insight
                or len(insight) < 15
                or insight.startswith("客观事实")
            ):
                logger.info(
                    f"[practice 熔断] 洞察无效: {event.get('entity', '?')} | "
                    f"insight={insight[:40] or '<empty>'}"
                )
                continue
            quality_events.append(event)
        valid_events = quality_events
        melted = before_melt - len(valid_events)
        if melted > 0:
            logger.info(f"Practice 洞察熔断: {melted} 条因无有效洞察被废弃")
        if not valid_events:
            logger.info("Practice 熔断：本周所有实践事件洞察均不合格，生成占位报告。")

        # ── 2. 同公司同质化事件语义合并 ──
        pre_merge = len(valid_events)
        valid_events = cls._merge_same_company_events(valid_events)
        if len(valid_events) < pre_merge:
            logger.info(f"Practice 语义合并: {pre_merge} -> {len(valid_events)}")

        # ── 2.5. 静默阻断：本周无实践事件 ──
        if not valid_events:
            logger.info(f"Practice 模式: 本周分析 {len(all_events)} 篇，无可复制的良好实践，执行静默阻断。")
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            placeholder = "\n".join([
                "# 🌱 同业良好实践周报 (Industry Best Practice Weekly)",
                "",
                f"> 📅 **生成时间**: {now_str}",
                f"> 📊 **实践总数**: 0 条",
                f"> 📰 **扫描样本**: {len(all_events)} 篇",
                "",
                "---",
                "",
                "## 📑 本周无可复制的良好实践",
                "",
                "本周扫描未发现可用于同业对标的创新实践或合规标杆案例。",
                "系统将继续每周监控行业优秀做法。",
                "",
                "---",
                "",
                "🤖 *本报告由 ESG Intelligence Agent 自动生成，数据来源于公开新闻源。*",
            ])
            Path(report_path).write_text(placeholder, encoding="utf-8")
            logger.info(f"Practice 静默阻断占位报告已写入: {report_path}")
            return [], []

        # ── 3. 按实践分类分组（经 map_practice_category 归一化） ──
        from notion_mapping import map_practice_category as _norm_cat
        for event in valid_events:
            raw_cat = str(event.get("practice_category", event.get("risk_category", ""))).strip()
            event["practice_category"] = _norm_cat(raw_cat)

        categorized: dict[str, list[dict]] = {k: [] for k in cls.PRACTICE_CATEGORY_ORDER}
        for event in valid_events:
            cat = str(event.get("practice_category", "")).strip()
            if cat in categorized:
                categorized[cat].append(event)
            else:
                categorized.setdefault("ESG披露与治理", []).append(event)

        # ── 4. 推断时间跨度 ──
        all_dates = [e.get("date", "") for e in valid_events]
        dmax = max(all_dates) if all_dates else datetime.now().strftime("%Y-%m-%d")
        # 实践周报：start_date 严格 = end_date - 7d，确保表头显示 7 天跨度
        end_dt = datetime.strptime(dmax, "%Y-%m-%d") if dmax != "?" else datetime.now()
        dmin = (end_dt - timedelta(days=7)).strftime("%Y-%m-%d")
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # ── 5. 生成 Markdown 报告 ──
        lines: list[str] = [
            f"# 🌱 同业良好实践周报 (Industry Best Practice Weekly)",
            "",
            f"> 📅 **生成时间**: {now_str}",
            f"> 📊 **实践总数**: {len(valid_events)} 条 | 涉及企业: {len(set(e.get('entity', '') for e in valid_events))} 家",
            f"> 📆 **覆盖时段**: {dmin} ~ {dmax}",
            "",
            "---",
            "",
            "## 📑 目录",
            "",
        ]

        for cat_key in cls.PRACTICE_CATEGORY_ORDER:
            evs = categorized.get(cat_key, [])
            if not evs:
                continue
            emoji = cls.PRACTICE_CATEGORY_EMOJI_MAP.get(cat_key, "")
            lines.append(f"- {emoji} **{cat_key}**（{len(evs)} 条）")
        lines.append("")

        lines += ["---", ""]

        for cat_key in cls.PRACTICE_CATEGORY_ORDER:
            evs = categorized.get(cat_key, [])
            if not evs:
                continue
            emoji = cls.PRACTICE_CATEGORY_EMOJI_MAP.get(cat_key, "")
            desc = cls.PRACTICE_CATEGORY_DESCRIPTIONS.get(cat_key, "")
            lines.append(f"## {emoji} {cat_key}")
            lines.append(f"> {desc}")
            lines.append("")

            evs.sort(key=lambda e: str(e.get("date", "")), reverse=True)

            for e in evs:
                entity = str(e.get("entity", "")).strip()
                title_text = str(
                    e.get("display_title_zh")
                    or e.get("core_event_title_en")
                    or ""
                ).strip()
                learning = str(e.get("learning_insight", "")).strip()
                date = str(e.get("date", ""))[:10]
                is_replicable = bool(e.get("is_replicable", False))
                replicable_badge = "✅ 可借鉴" if is_replicable else "📋 参考了解"

                sources_raw = e.get("sources", [])
                source_links: list[str] = []
                seen_labels: set[str] = set()
                if isinstance(sources_raw, list):
                    for s in sources_raw:
                        if isinstance(s, dict):
                            name = str(s.get("name", "")).strip()
                            src_url = str(s.get("url", "")).strip()
                            if name and name not in seen_labels:
                                seen_labels.add(name)
                                if src_url and src_url.startswith("http"):
                                    source_links.append(f"[{name}]({src_url})")
                                else:
                                    source_links.append(name)
                if len(source_links) > 2:
                    sources_str = " · ".join(source_links[:2]) + f" · 等 {len(source_links)} 家"
                elif source_links:
                    sources_str = " · ".join(source_links)
                else:
                    sources_str = "Unknown"

                lines.append(f"**{entity}** | *{title_text}*")
                lines.append(f"> 📅 {date} | 📰 {sources_str}")
                if learning:
                    lines.append(f"> 💡 **可借鉴点**：{learning}")
                lines.append(f"> {replicable_badge}")
                lines.append("")
                lines.append("---")
                lines.append("")

        lines += [
            "---",
            "",
            "🤖 *本报告由 ESG Intelligence Agent | Practice 轨道自动生成。*",
            "⚠️  *仅供内部学习参考，不构成投资或法律建议。*",
        ]

        report_md = "\n".join(lines)
        Path(report_path).write_text(report_md, encoding="utf-8")
        logger.info(f"Practice report written: {report_path} ({len(valid_events)} valid)")

        # 返回兼容格式
        compatible: list[dict] = []
        for e in valid_events:
            sources_raw = e.get("sources", [])
            if isinstance(sources_raw, list):
                source_names = [str(s.get("name", "")) for s in sources_raw if isinstance(s, dict)]
                source_str = ", ".join(n for n in source_names if n)
            else:
                source_str = str(sources_raw) if sources_raw else "Unknown"
            cat = str(e.get("practice_category", "")).strip()
            title_val = str(e.get("display_title_zh") or e.get("core_event_title_en", "")).strip()
            compatible.append({
                "company": str(e.get("entity", "")).strip(),
                "title": title_val,
                "insight": str(e.get("learning_insight", "")).strip(),
                "source": source_str,
                "date": str(e.get("date", ""))[:10],
                "tags": [cat] if cat else ["良好实践"],
                "url": str(e.get("url", "")).strip(),
            })
        return compatible, valid_events

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

    def _llm_global_convergence(self, valid_events: list[dict]) -> list[dict]:
        """终极 LLM 全局聚合层 — 解决跨批次 Semantic Drift 导致的假性重复。"""
        if not valid_events or len(valid_events) <= 1:
            return valid_events

        events_json = json.dumps(valid_events, ensure_ascii=False, indent=2)
        system_msg = """You are an event dedup engine. Merge events describing the same core incident.
Rules: 1) Extract best display_title_zh. 2) Merge all sources preserving lang tags.
3) Keep latest date and best insight. 4) Output same array structure. 5) If no dupes, return as-is.
Output only valid JSON array, no markdown."""

        try:
            raw = self._call_llm_cheap(
                system_msg,
                f"Events JSON:\n{events_json}",
                temperature=0.0, max_tokens=4096,
                response_format={"type": "json_object"},
            )
            if raw is None:
                return valid_events
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

        # practice 模式：使用实践企业名单和实践 prompt
        if mode == "practice":
            company_names = self.config.get_practice_company_display_names()
            system_prompt = self._build_practice_system_prompt(company_names, mode)
        else:
            company_names = self.config.get_all_company_display_names()
            system_prompt = self._build_system_prompt(company_names, mode)

        total = len(raw_data_list)
        batch_count = (total + self.BATCH_SIZE - 1) // self.BATCH_SIZE
        logger.info(
            f"LLM processing: {total} articles -> {batch_count} batch(es) "
            f"(BATCH_SIZE={self.BATCH_SIZE}, mode={mode})"
        )

        all_events: list[dict] = []

        for batch_idx in range(batch_count):
            start = batch_idx * self.BATCH_SIZE
            end = min(start + self.BATCH_SIZE, total)
            batch = raw_data_list[start:end]
            logger.info(f"  Batch {batch_idx + 1}/{batch_count}: articles #{start + 1}-{end} ({len(batch)} items)")

            user_message = self._build_articles_text(batch, start)

            try:
                raw_output = self._call_llm(
                    system_prompt, user_message,
                    temperature=0.1, max_tokens=8192,
                    response_format={"type": "json_object"},
                )

                if raw_output is None:
                    logger.warning(
                        f"  ⚠ Batch {batch_idx + 1} LLM call returned None — "
                        f"DISCARDING {len(batch)} articles."
                    )
                    continue

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

    # ── 钉钉推送格式化 ──────────────────────────────────────

    @staticmethod
    def _get_severity_marker(category: str, is_material: bool) -> str:
        """根据风险类别和材料冲击判定严重度标记。"""
        if category in ("供应链断裂预警", "合规与运营危机") and is_material:
            return "🔥"
        elif category in ("政策与市场准入", "早期合规预警") and is_material:
            return "⚡"
        else:
            return "⚠️"

    @classmethod
    def _format_for_dingtalk(cls, valid_events: list[dict], mode: str, now_str: str, dmin: str, dmax: str, weekly_review_text: str = None) -> str:
        """从结构化事件数据构建钉钉优化版 Markdown 消息。"""
        # ── 按风险类别分组 ──
        categorized: dict[str, list[dict]] = {k: [] for k in cls.CATEGORY_ORDER}
        for e in valid_events:
            cat = str(e.get("risk_category", "")).strip()
            if cat == "市场准入预警":
                cat = "政策与市场准入"
            if cat in categorized:
                categorized[cat].append(e)
            else:
                categorized.setdefault("合规与运营危机", []).append(e)

        # 组内按日期降序
        for cat in cls.CATEGORY_ORDER:
            categorized[cat].sort(key=lambda e: str(e.get("date", "")), reverse=True)

        # ── 标题 ──
        if mode == "weekly":
            title = "🏛️ ESG 全球地缘与合规周报 (Weekly Strategy Insight)"
        else:
            title = "🏛️ ESG 全球供应链动态日报 (Daily Intelligence)"

        n_events = len(valid_events)
        n_companies = len(set(e.get("entity", "") for e in valid_events))

        lines: list[str] = [
            f"# {title}",
            f"> 📅 {now_str} · 📊 {n_events} 条情报 · {n_companies} 家企业 · 📆 {dmin} ~ {dmax}",
            "",
        ]

        # ── 目录 ──
        lines.append("## 📑 目录")
        lines.append("")
        for cat in cls.CATEGORY_ORDER:
            evs = categorized.get(cat, [])
            if not evs:
                continue
            emoji = cls.CATEGORY_EMOJI_MAP.get(cat, "")
            lines.append(f"- {emoji} **{cat}** · {len(evs)} 条")
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("")

        # ── 各分类内容 ──
        for cat in cls.CATEGORY_ORDER:
            evs = categorized.get(cat, [])
            if not evs:
                continue
            emoji = cls.CATEGORY_EMOJI_MAP.get(cat, "")
            desc = cls.CATEGORY_DESCRIPTIONS.get(cat, "")
            lines.append(f"## {emoji} {cat}")
            lines.append(f"> {desc}")
            lines.append("")

            for e in evs:
                entity = str(e.get("entity", "")).strip()
                title_text = str(
                    e.get("display_title_zh")
                    or e.get("core_event_title_en")
                    or e.get("core_event_title", "")
                ).strip()
                insight = str(e.get("executive_insight", "")).strip()
                date = str(e.get("date", ""))[:10]
                is_material = bool(e.get("is_direct_material_impact", False))

                severity = cls._get_severity_marker(cat, is_material)

                # ── 信息源聚合 + 折叠 ──
                sources_raw = e.get("sources", [])
                event_lang = str(e.get("original_language", "")).strip()
                source_links: list[str] = []
                seen_labels: set[str] = set()
                if isinstance(sources_raw, list):
                    for s in sources_raw:
                        if isinstance(s, dict):
                            name = str(s.get("name", "")).strip()
                            src_url = str(s.get("url", "")).strip()
                            s_lang = str(s.get("original_language", "")) or event_lang
                            if name:
                                label = f"{name} ({s_lang})" if s_lang else name
                                if label in seen_labels:
                                    continue
                                seen_labels.add(label)
                                if src_url and src_url.lower().startswith("http"):
                                    source_links.append(f"[{label}]({src_url})")
                                else:
                                    source_links.append(label)
                        elif isinstance(s, str) and s.strip():
                            if s.strip() not in seen_labels:
                                seen_labels.add(s.strip())
                                source_links.append(s.strip())

                if len(source_links) > 2:
                    visible = source_links[:2]
                    total = len(source_links)
                    sources_str = " · ".join(visible) + f" · 等 {total} 家"
                elif source_links:
                    sources_str = " · ".join(source_links)
                else:
                    sources_str = "Unknown"

                lines.append(f"{severity} **{entity} | {title_text}**")
                lines.append("")
                lines.append(f"{insight}")
                lines.append("")
                lines.append(f"📅 {date} · 📰 {sources_str}")
                lines.append("")
                lines.append("━━━━━━━━━━━━━━━━━━━━")
                lines.append("")

        # ── 周度盲区分析 (weekly only) ──
        if mode == "weekly" and weekly_review_text:
            # 盲区分析可能很长，在钉钉中做适度截断保留核心内容
            review = weekly_review_text.strip()
            if len(review) > 3000:
                review = review[:3000] + "\n\n> ⚠️ 盲区分析过长已截断，完整内容请查看周报文件。"
            lines.append("## 🔍 监控矩阵盲区分析")
            lines.append("")
            lines.append(review)
            lines.append("")
            lines.append("━━━━━━━━━━━━━━━━━━━━")
            lines.append("")

        # ── 页脚 ──
        lines.append("🤖 由 ESG Intelligence Agent 自动生成 · 仅供决策参考")

        ding_content = "\n".join(lines)

        # 截断保护
        if len(ding_content) > 15000:
            ding_content = ding_content[:15000] + "\n\n> ⚠️ 报告过长，已自动截断。完整内容请查看源文件。"

        return ding_content

    @classmethod
    def _format_practice_for_dingtalk(
        cls, valid_events: list[dict], now_str: str, dmin: str, dmax: str,
    ) -> str:
        """从实践事件构建钉钉推送 Markdown 消息。"""
        # ── 按实践类别分组（已由 report 阶段归一化，此处分组为防御性） ──
        from notion_mapping import map_practice_category as _norm_cat
        categorized: dict[str, list[dict]] = {k: [] for k in cls.PRACTICE_CATEGORY_ORDER}
        for e in valid_events:
            cat = _norm_cat(str(e.get("practice_category", "")).strip())
            if cat in categorized:
                categorized[cat].append(e)
            else:
                categorized.setdefault("ESG披露与治理", []).append(e)
        for cat in cls.PRACTICE_CATEGORY_ORDER:
            categorized[cat].sort(key=lambda e: str(e.get("date", "")), reverse=True)

        total_entities = len(set(e.get("entity", "") for e in valid_events))
        replicable_count = sum(1 for e in valid_events if e.get("is_replicable"))

        lines: list[str] = [
            f"# 🌱 同业良好实践周报",
            f"> 📅 {now_str} | 📊 {len(valid_events)} 条实践",
            f"> 🏢 {total_entities} 家企业 | ✅ {replicable_count} 条可借鉴",
            f"> 📆 {dmin} ~ {dmax}",
            "",
            "---",
            "",
        ]

        for cat in cls.PRACTICE_CATEGORY_ORDER:
            evs = categorized.get(cat, [])
            if not evs:
                continue
            emoji = cls.PRACTICE_CATEGORY_EMOJI_MAP.get(cat, "")
            lines.append(f"## {emoji} {cat}")
            lines.append(f"> {len(evs)} 条")
            lines.append("")

            for e in evs[:5]:  # 每类最多 5 条，避免钉钉消息过长
                entity = str(e.get("entity", "")).strip()
                title_text = str(
                    e.get("display_title_zh") or e.get("core_event_title_en", "")
                ).strip()
                learning = str(e.get("learning_insight", "")).strip()
                date = str(e.get("date", ""))[:10]
                is_rep = "✅" if e.get("is_replicable") else "📋"

                lines.append(f"**{entity}** | *{title_text}*")
                lines.append(f"> 📅 {date}")
                if learning and learning != "（待进一步分析）":
                    lines.append(f"> 💡 **可借鉴点**：{learning}")
                lines.append(f"> {is_rep}")
                lines.append("")

            remaining = len(evs) - 5
            if remaining > 0:
                lines.append(f"> *...等 {remaining} 条，完整内容见报告*")
                lines.append("")

        # 页脚
        lines.append("🤖 由 ESG Intelligence Agent · Practice 轨道自动生成")

        ding_content = "\n".join(lines)
        if len(ding_content) > 15000:
            ding_content = ding_content[:15000] + "\n\n> ⚠️ 报告过长已截断。完整内容请查看源文件。"

        return ding_content

    # ── 钉钉推送 ──────────────────────────────────────────

    def push_to_dingtalk(self, report_path: str = "esg_global_report.md", mode: str = "daily") -> None:
        webhook = os.environ.get("DINGTALK_WEBHOOK")
        if not webhook:
            logger.info("未配置钉钉 Webhook (DINGTALK_WEBHOOK)，跳过推送。")
            return

        try:
            valid_events = self._last_valid_events

            if mode == "practice":
                first_line = "🌱【同业实践】良好实践学习"
                ding_title = "🌱 同业良好实践周报"
            elif mode == "weekly":
                first_line = "🔮【宏观合规战略】全球地缘与准入壁垒周报"
                ding_title = "🏛️ ESG 全球地缘与合规周报"
            else:
                first_line = "全球供应链合规与风险速递"
                ding_title = "🏛️ ESG 全球供应链动态日报 (Daily Intelligence)"

            if not valid_events:
                if mode == "practice":
                    ding_content = (
                        f"# {first_line}\n\n"
                        f"> ✅ **本周无新增同业良好实践。**\n"
                        f"> 📅 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    )
                    logger.info("本周无实践事件，发送「无实践」心跳通知。")
                else:
                    ding_content = (
                        f"# {first_line}\n\n"
                        f"> ✅ **系统巡检完成，今日无新增风险事件。**\n"
                        f"> 📅 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    )
                    logger.info("今日无风险事件，发送「无风险」心跳通知。")
            else:
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                all_dates = [e.get("date", "") for e in valid_events]
                dmax = max(all_dates) if all_dates else datetime.now().strftime("%Y-%m-%d")
                # 周报/实践模式：start_date = end_date - 7d
                if mode in ("weekly", "practice"):
                    end_dt = datetime.strptime(dmax, "%Y-%m-%d") if dmax != "?" else datetime.now()
                    dmin = (end_dt - timedelta(days=7)).strftime("%Y-%m-%d")
                else:
                    dmin = min(all_dates) if all_dates else dmax
                if mode == "practice":
                    ding_content = self._format_practice_for_dingtalk(
                        valid_events, now_str, dmin, dmax,
                    )
                else:
                    ding_content = self._format_for_dingtalk(
                        valid_events, mode, now_str, dmin, dmax,
                        weekly_review_text=self._weekly_review_text if mode == "weekly" else None,
                    )

            # 底部追加 Notion 数据库链接
            db_id = os.environ.get(
                "NOTION_PRACTICE_DATABASE_ID" if mode == "practice" else "NOTION_DATABASE_ID", ""
            )
            if db_id:
                notion_url = f"https://app.notion.com/p/fangxie/{db_id}"
                ding_content += f"\n\n📋 [查看完整数据库]({notion_url})"

            headers = {"Content-Type": "application/json"}
            data = {"msgtype": "markdown", "markdown": {"title": ding_title, "text": ding_content}}
            logger.info("正在向钉钉发送情报简报...")
            response = requests.post(webhook, headers=headers, data=json.dumps(data))
            logger.info(f"钉钉服务器返回: {response.text}")
        except Exception as exc:
            logger.error(f"钉钉推送失败: {exc}")

    # ── Notion 推送 ────────────────────────────────────────

    def push_to_notion(self, mode: str = "daily") -> None:
        """将结构化事件幂等写入 Notion 数据库（通过 External ID upsert 去重）。"""
        token = os.environ.get("NOTION_TOKEN", "")
        database_id = os.environ.get("NOTION_DATABASE_ID", "")

        if not token or not database_id:
            logger.info("未配置 Notion (NOTION_TOKEN / NOTION_DATABASE_ID)，跳过推送。")
            return

        valid_events = self._last_valid_events
        if not valid_events:
            logger.info("无风险事件，跳过 Notion 推送。")
            return

        try:
            notion = NotionClient(auth=token)
            today_str = datetime.now().strftime("%Y-%m-%d")
            success_count = 0
            fail_count = 0

            logger.info(f"开始向 Notion 写入 {len(valid_events)} 条事件 (database={database_id[:8]}...)...")

            for i, event in enumerate(valid_events):
                try:
                    # 统一字段命名（兼容 upsert 助手）
                    event["mode"] = event.get("mode", mode)
                    event["push_date"] = event.get("push_date", today_str)
                    # 字段别名
                    if "english_title" not in event:
                        event["english_title"] = event.get("core_event_title_en", "")
                    if "insight" not in event:
                        event["insight"] = event.get("executive_insight", "")

                    action, page_id = upsert_notion_page(
                        event, notion, database_id, dry_run=False,
                    )
                    if action in ("created", "updated"):
                        success_count += 1
                    logger.info(
                        f"  [{i + 1}/{len(valid_events)}] {action} | "
                        f"{event.get('entity', '?')} | "
                        f"{event.get('display_title_zh', '?')[:40]}"
                    )
                except Exception as item_exc:
                    fail_count += 1
                    logger.warning(f"Notion 写入失败 (event: {event.get('display_title_zh', '?')}): {item_exc}")

            logger.info(
                f"Notion 推送完成: ✅ {success_count} 成功 / ❌ {fail_count} 失败 / 共 {len(valid_events)} 条"
            )
        except Exception as exc:
            logger.error(f"Notion 推送整体失败: {exc}")

    def push_practice_to_notion(self, mode: str = "practice") -> None:
        """将实践事件幂等写入独立的 Practice Notion 数据库。"""
        token = os.environ.get("NOTION_TOKEN", "")
        database_id = os.environ.get("NOTION_PRACTICE_DATABASE_ID", "")

        if not token or not database_id:
            logger.info("未配置 Practice Notion (NOTION_PRACTICE_DATABASE_ID)，跳过推送。")
            return

        valid_events = self._last_valid_events
        if not valid_events:
            logger.info("无实践事件，跳过 Practice Notion 推送。")
            return

        try:
            from notion_upsert import upsert_practice_page

            notion = NotionClient(auth=token)
            today_str = datetime.now().strftime("%Y-%m-%d")
            success_count = 0
            fail_count = 0

            logger.info(f"开始向 Practice Notion 写入 {len(valid_events)} 条事件 (database={database_id[:8]}...)...")

            for i, event in enumerate(valid_events):
                try:
                    event["mode"] = event.get("mode", mode)
                    event["push_date"] = event.get("push_date", today_str)
                    if "english_title" not in event:
                        event["english_title"] = event.get("core_event_title_en", "")
                    if "insight" not in event:
                        event["insight"] = event.get("learning_insight", "")

                    action, page_id = upsert_practice_page(
                        event, notion, database_id, dry_run=False,
                    )
                    if action in ("created", "updated"):
                        success_count += 1
                    logger.info(
                        f"  [{i + 1}/{len(valid_events)}] {action} | "
                        f"{event.get('entity', '?')} | "
                        f"{event.get('display_title_zh', '?')[:40]}"
                    )
                except Exception as item_exc:
                    fail_count += 1
                    logger.warning(f"Practice Notion 写入失败: {item_exc}")

            logger.info(
                f"Practice Notion 推送完成: ✅ {success_count} 成功 / ❌ {fail_count} 失败 / 共 {len(valid_events)} 条"
            )
        except Exception as exc:
            logger.error(f"Practice Notion 推送整体失败: {exc}")

    # ── 入口 ─────────────────────────────────────────────

    def run(self, mode: str = "daily", report_path: str = "esg_global_report.md") -> None:
        if mode == "practice":
            report_path = "esg_practice_report.md"
        t0 = time.monotonic()
        logger.info(f"═══ ESG Intelligence Agent v9 | Mode: {mode.upper()} ═══")

        # ── 重置 Token 统计 ─────────────────────────
        self._reset_token_stats()

        query_tasks = self.config.build_query_tasks(mode)
        logger.info(f"Query tasks: {len(query_tasks)} (mode={mode})")

        # ── Phase 0.5: AI 动态搜索词生成 ─────────────
        # practice 模式跳过此阶段（AI 发现专为风险监控设计）
        ai_discovery_urls: list[dict] = []
        if mode != "practice":
            company_names = self.config.get_all_company_display_names()
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
        # practice/weekly 使用 7 天窗口，daily 使用 24 小时窗口
        dyn_time_window = "7d" if mode in ("weekly", "practice") else "24h"
        dynamic_items = engine.fetch_from_prebuilt_urls(dynamic_urls, time_window=dyn_time_window)

        # 1c. AI 动态发现查询（仅 daily 模式触发，使用对应时间窗）
        ai_time_window = "7d" if mode in ("weekly", "practice") else "24h"
        ai_items = engine.fetch_from_prebuilt_urls(ai_discovery_urls, time_window=ai_time_window)

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
            if mode == "practice":
                title = "🌱 同业良好实践周报 (Industry Best Practice Weekly)"
                first_line = "本周未捕获同业实践数据"
            elif mode == "weekly":
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

        # ── Phase 3: Deep Content Extraction (并发版) ────────────
        logger.info(f"Phase 3: Extracting body text from {len(self.articles)} articles (concurrent)...")

        from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _as_completed

        def _extract_one(idx_article):
            idx, article = idx_article
            body = ContentExtractor.extract(article.url)
            return idx, body

        _EXTRACT_WORKERS = 10  # 正文抓取比 RSS 更重，保守设置 10 workers
        extracted_count = 0

        with _TPE(max_workers=_EXTRACT_WORKERS) as executor:
            futures = {
                executor.submit(_extract_one, (idx, article)): article
                for idx, article in enumerate(self.articles)
            }
            done_count = 0
            for future in _as_completed(futures):
                try:
                    idx, body = future.result()
                    article = self.articles[idx]
                    if body:
                        article.raw_summary = body
                        extracted_count += 1
                    else:
                        desc = article.description.strip()
                        if desc and desc != article.title and len(desc) > 15:
                            article.raw_summary = desc[:200]
                except Exception:
                    pass
                done_count += 1
                if done_count % 20 == 0:
                    logger.info(f"  Progress: {done_count}/{len(self.articles)}")

        logger.info(f"Phase 3 done. Body text extracted: {extracted_count}/{len(self.articles)}")

        # ── Phase 2.4: Spam/垃圾过滤 ───────────
        pre_spam = len(self.articles)
        self.articles = [
            a for a in self.articles
            if not EntityFilter.is_spam(a.title, a.url, a.raw_summary)
        ]
        spam_count = pre_spam - len(self.articles)
        if spam_count:
            logger.info(f"Phase 2.4 spam filter: {pre_spam} -> {len(self.articles)} ({spam_count} spam)")

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

        # ── Phase 3: Tavily 相关性评分 (optional, 有 API key 时启用) ──
        if self._tavily_scorer is not None:
            pre_tavily = len(self.articles)
            tavily_articles = []
            for art in self.articles:
                score = self._tavily_scorer.score_article(
                    art.title, art.raw_summary or "", art.url
                )
                if score >= self._tavily_scorer._threshold:
                    tavily_articles.append(art)
                else:
                    logger.info(
                        f"[Tavily] 已过滤(score={score:.2f}): {art.title[:80]}"
                    )
            self.articles = tavily_articles
            logger.info(
                f"Phase 3 Tavily scoring: {pre_tavily} -> {len(self.articles)} "
                f"(filtered {pre_tavily - len(self.articles)} low-relevance)"
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
        if mode == "practice":
            intelligence_json, valid_events = self._generate_practice_report_and_filter(
                all_v10_events, mode, report_path,
            )
        else:
            intelligence_json, valid_events = self._generate_v10_report_and_filter(all_v10_events, mode, report_path)
        self._last_valid_events = valid_events
        logger.info(f"Phase 5 done. Python pipeline: {len(intelligence_json)} valid items -> {report_path}")

        # ── 报告归档 (按日期) ───────────────────────────────
        try:
            archive_dir = Path(report_path).parent / "reports"
            archive_dir.mkdir(exist_ok=True)
            today_str = datetime.now().strftime("%Y-%m-%d")
            archive_path = archive_dir / f"{today_str}_{mode}.md"
            import shutil
            shutil.copy2(report_path, archive_path)
            logger.info(f"Report archived to: {archive_path}")

            # PDF 归档
            pdf_path = archive_dir / f"{today_str}_{mode}.pdf"
            convert_markdown_to_pdf(str(report_path), str(pdf_path))
            logger.info(f"PDF archived to: {pdf_path}")
        except Exception as e:
            logger.warning(f"Report archiving/PDF failed: {e}")

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
                weekly_review = self._weekly_threat_landscape_review(all_v10_events, report_path, token_summary)
                self._weekly_review_text = weekly_review
            except Exception as e:
                self._weekly_review_text = None
                logger.warning(f"Weekly threat review failed ({e}), weekly report unaffected")

        # ── 运行指标收集 ─────────────────────────────────
        self._collect_metrics(mode, elapsed, len(all_v10_events), len(valid_events), len(intelligence_json))

    def _collect_metrics(self, mode: str, elapsed: float, total_events: int, valid_events: int, final_items: int) -> None:
        """收集并持久化本次运行的监控指标。"""
        try:
            from radar_infra.support import RunMetrics, MetricsStore
            risk_dist = {}
            material_events = 0
            for e in self._last_valid_events:
                if mode == "practice":
                    cat = str(e.get("practice_category", "unknown")).strip()
                    risk_dist[cat] = risk_dist.get(cat, 0) + 1
                else:
                    cat = str(e.get("risk_category", "unknown")).strip()
                    risk_dist[cat] = risk_dist.get(cat, 0) + 1
            if mode == "practice":
                material_events = sum(1 for e in self._last_valid_events if e.get("is_replicable"))
            else:
                material_events = sum(1 for e in self._last_valid_events if e.get("is_direct_material_impact"))

            metrics = RunMetrics(
                timestamp=datetime.now().isoformat(),
                mode=mode,
                elapsed_seconds=elapsed,
                total_raw_items=len(self.articles),
                llm_input_tokens=self._llm_usage.prompt_tokens,
                llm_output_tokens=self._llm_usage.completion_tokens,
                llm_total_tokens=self._llm_usage.total_tokens,
                llm_cost_usd=self._llm_usage.total_cost_usd,
                total_events=total_events,
                valid_events=valid_events,
                material_events=material_events,
                final_report_items=final_items,
                risk_distribution=risk_dist,
            )
            metrics.log_summary()
            MetricsStore().append(metrics)
        except Exception as e:
            logger.warning(f"Metrics collection failed (non-critical): {e}")

    def __init__(self, config_path: str = None):
        self.config = AgentConfig.from_yaml(config_path)
        self.articles: list[NewsArticle] = []
        self._seen_urls: set[str] = set()
        self._last_valid_events: list[dict] = []
        self._weekly_review_text: Optional[str] = None
        self._llm_usage = TokenUsage()
        # Tavily 相关性评分（可选，API key 未配时自动降级）
        self._tavily_scorer = create_scorer()
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
        description="ESG Intelligence Agent — 多模动态播报 (daily/weekly/practice)",
    )
    parser.add_argument(
        "--mode",
        choices=["daily", "weekly", "practice"],
        default="daily",
        help="运行模式：daily=日常舆情（默认）/ weekly=宏观政策+全部主题 / practice=同业良好实践周报",
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
        help="跳过钉钉与 Notion 推送",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    agent = ESGIntelligenceAgent(config_path=args.config)
    agent.run(mode=args.mode, report_path=args.report)
    if not args.no_push:
        agent.push_to_dingtalk(mode=args.mode)
        if args.mode == "practice":
            agent.push_practice_to_notion(mode=args.mode)
        else:
            agent.push_to_notion(mode=args.mode)
    else:
        logger.info("钉钉与 Notion 推送已跳过（--no-push）。")
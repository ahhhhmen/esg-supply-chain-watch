"""
notion_mapping.py — Notion Risk Category 分类映射工具
═══════════════════════════════════════════════════════════════════════════════
将 agent 生成的 5 个原始分类映射为 Notion 数据库的 6 个新分类：

原始分类（agent 产出）       →  Notion 新分类
─────────────────────────────────────────────────
供应链断裂预警               →  供应链断裂
政策与市场准入               →  政策壁垒
合规与运营危机               →  按事件内容拆分（劳工权益 / 环保违规 / 治理合规）
早期合规预警                 →  治理合规
机构与声誉预警               →  声誉风险
═══════════════════════════════════════════════════════════════════════════════
"""

import re

# ── Notion 数据库属性名常量 ─────────────────────────────────────

EXTERNAL_ID = "External ID"
"""Notion 数据库中用于持久化去重的属性名（类型：Text）。"""

# 直接映射（无需内容判断的分类）
_CATEGORY_DIRECT_MAP = {
    "供应链断裂预警": "供应链断裂",
    "政策与市场准入": "政策壁垒",
    "市场准入预警": "政策壁垒",  # LLM 偶尔会输出这个别名
    "早期合规预警": "治理合规",
    "机构与声誉预警": "声誉风险",
}

# 合规与运营危机 → 按关键词拆分
# 顺序很重要：先判断劳工，再判断环保，最后兜底治理合规
_LABOR_KEYWORDS = [
    "罢工", "工会", "劳工", "劳资", "裁员", "抗议", "工潮", "员工",
    "strike", "union", "labor", "labour", "layoff", "protest", "worker",
    "walkout", "UAW", "IF Metall",
]

_ENVIRONMENT_KEYWORDS = [
    "污染", "排放", "泄漏", "废水", "废气", "环保", "环境", "毒",
    "pollution", "emission", "leak", "spill", "environmental", "toxic",
    "contamination", "waste",
]


def map_risk_category(original_category: str, event_title: str = "",
                      event_insight: str = "") -> str:
    """
    将 agent 原始分类映射为 Notion 新分类。

    Args:
        original_category: agent 生成的原始分类名（如 "供应链断裂预警"）
        event_title: 事件标题（用于"合规与运营危机"的拆分判断）
        event_insight: 事件洞察（辅助拆分判断）

    Returns:
        Notion 新分类名（6 选 1）
    """
    if not original_category:
        return "治理合规"

    original_category = original_category.strip()

    # 1. 直接映射
    if original_category in _CATEGORY_DIRECT_MAP:
        return _CATEGORY_DIRECT_MAP[original_category]

    # 2. 合规与运营危机 → 按内容拆分
    if original_category == "合规与运营危机":
        combined_text = f"{event_title} {event_insight}".lower()

        # 先判断劳工权益
        for kw in _LABOR_KEYWORDS:
            if kw.lower() in combined_text:
                return "劳工权益"

        # 再判断环保违规
        for kw in _ENVIRONMENT_KEYWORDS:
            if kw.lower() in combined_text:
                return "环保违规"

        # 兜底：产品召回、安全事故、车辆起火等 → 治理合规
        return "治理合规"

    # 3. 未知分类兜底
    return "治理合规"


def map_entity(original_entity: str) -> str:
    """
    实体名称归一化（可选）。
    目前 Notion 允许 select 字段自动创建新选项，这里仅做轻量清理。
    """
    if not original_entity:
        return "其他"
    return original_entity.strip()

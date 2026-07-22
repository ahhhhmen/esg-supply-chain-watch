"""
Unit tests for materiality guardrails and unified deduplication fixes.
"""

from __future__ import annotations

from esg_intelligence_agent import ESGIntelligenceAgent


def test_oem_wage_overtime_downgraded_to_watch():
    event = {
        "entity": "梅赛德斯-奔驰",
        "core_event_title_en": "Mercedes-Benz asks German workers for unpaid overtime and wage cuts to reduce output",
        "display_title_zh": "梅赛德斯-奔驰要求德国员工无偿加班并削减薪酬降低产量",
        "executive_insight": "梅赛德斯-奔驰要求德国员工无偿延长工时并削减薪酬降低产量...可能影响其电动车产量，进而间接冲击华友钴业作为电池材料供应商的订单稳定性。",
        "risk_category": "合规与运营危机",
        "is_valid_risk": True,
        "is_direct_material_impact": True,  # LLM initially misclassified
        "sources": [{"name": "За рулем", "url": "https://example.com/ru"}]
    }

    processed = ESGIntelligenceAgent._apply_materiality_guardrails(event)
    assert processed["is_direct_material_impact"] is False
    assert processed["materiality"] == "🟡 战略观察"


def test_gm_unifor_negotiation_downgraded_to_watch():
    event = {
        "entity": "通用汽车",
        "core_event_title_en": "Unifor designates GM as next bargaining target",
        "display_title_zh": "Unifor工会指定通用汽车为下一轮集体谈判目标",
        "executive_insight": "Unifor工会将通用汽车列为下一轮集体谈判目标...若罢工导致减产，将间接影响华友前驱体订单交付节奏。",
        "risk_category": "合规与运营危机",
        "is_valid_risk": True,
        "is_direct_material_impact": True,  # LLM initially misclassified
        "sources": [{"name": "Les Affaires", "url": "https://example.com/fr"}]
    }

    processed = ESGIntelligenceAgent._apply_materiality_guardrails(event)
    assert processed["is_direct_material_impact"] is False
    assert processed["materiality"] == "🟡 战略观察"


def test_unified_deduplication_prevents_duplicate_across_valid_and_watch():
    event_red = {
        "entity": "通用汽车",
        "core_event_title_en": "Unifor designates GM as next bargaining target",
        "display_title_zh": "Unifor工会指定通用汽车为下一轮集体谈判目标",
        "executive_insight": "Unifor工会将通用汽车列为下一轮集体谈判目标...",
        "risk_category": "合规与运营危机",
        "date": "2026-07-21",
        "is_valid_risk": True,
        "is_direct_material_impact": True,
        "sources": [{"name": "Les Affaires", "url": "https://example.com/fr"}]
    }
    event_yellow = {
        "entity": "通用汽车",
        "core_event_title_en": "Unifor union to begin talks with GM next month",
        "display_title_zh": "Unifor工会下月与通用汽车展开谈判",
        "executive_insight": "Unifor工会下月与通用汽车展开谈判...",
        "risk_category": "合规与运营危机",
        "date": "2026-07-21",
        "is_valid_risk": True,
        "is_direct_material_impact": False,
        "sources": [{"name": "singtao", "url": "https://example.com/zh"}]
    }

    all_events = [event_red, event_yellow]
    valid_events, watch_events = ESGIntelligenceAgent._generate_v10_report_and_filter(
        all_events, mode="daily"
    )

    # Both events represent the same GM Unifor bargaining event, so they should merge!
    # Main report (valid_events) should have 0 items, watch_events should have 1 item with combined sources!
    assert len(valid_events) == 0
    assert len(watch_events) == 1
    assert len(watch_events[0]["sources"]) == 2

import pytest
from esg_intelligence_agent import ESGIntelligenceAgent

def test_tesla_test_drive_accident_claim_is_filtered_out():
    """验证终端个案交通事故/试驾索赔诉讼被物理降噪拦截为 is_valid_risk = False"""
    event = {
        "entity": "特斯拉",
        "core_event_title_en": "Woman sues Tesla for $10M after test drive crash",
        "display_title_zh": "女子在特斯拉试驾事故后索赔1000万美元",
        "executive_insight": "该事件属于终端事故诉讼，未涉及动力电池或上游材料。",
        "date": "2026-08-02",
        "sources": [{"name": "24 Канал", "url": "https://example.com/article1"}],
        "risk_category": "合规与运营危机",
        "is_valid_risk": True,
        "is_direct_material_impact": False,
    }

    result = ESGIntelligenceAgent._apply_materiality_guardrails(event)
    assert result.get("is_valid_risk") is False
    assert result.get("risk_category") == "无关噪音"
    assert "终端个案交通事故" in result.get("executive_insight", "")

def test_other_retail_lawsuits_and_accidents_are_filtered_out():
    """验证各类终端试驾事故、人身伤害索赔与买卖纠纷均被物理拦截"""
    events = [
        {
            "entity": "特斯拉",
            "core_event_title_en": "Tesla test drive accident leads to personal injury claim",
            "display_title_zh": "特斯拉试驾碰撞导致人身伤害索赔",
            "is_valid_risk": True,
        },
        {
            "entity": "宝马",
            "core_event_title_en": "BMW customer sues dealer over deposit refund dispute",
            "display_title_zh": "宝马车主就退定金纠纷起诉经销商",
            "is_valid_risk": True,
        },
        {
            "entity": "福特汽车",
            "core_event_title_en": "Ford test drive vehicle crash causes traffic injury",
            "display_title_zh": "福特试驾车发生车祸致人受伤",
            "is_valid_risk": True,
        },
    ]

    for ev in events:
        res = ESGIntelligenceAgent._apply_materiality_guardrails(ev)
        assert res.get("is_valid_risk") is False, f"Failed to filter out: {ev['display_title_zh']}"

def test_genuine_esg_risks_are_preserved():
    """验证真实的 ESG 供应链风险（罢工、矿山事故、监管调查）不受影响"""
    genuine_events = [
        {
            "entity": "特斯拉",
            "core_event_title_en": "IF Metall union expands Tesla Sweden strike",
            "display_title_zh": "瑞典 IF Metall 工会扩大针对特斯拉的罢工",
            "risk_category": "合规与运营危机",
            "is_valid_risk": True,
            "is_direct_material_impact": False,
        },
        {
            "entity": "宁德时代",
            "core_event_title_en": "CATL Yichun lithium mine halts mining operations",
            "display_title_zh": "宁德时代宜春锂矿暂停采矿作业",
            "risk_category": "供应链断裂预警",
            "is_valid_risk": True,
            "is_direct_material_impact": True,
        },
    ]

    for ev in genuine_events:
        res = ESGIntelligenceAgent._apply_materiality_guardrails(ev)
        assert res.get("is_valid_risk") is True, f"Incorrectly filtered genuine event: {ev['display_title_zh']}"

"""
盲区分析（weekly threat landscape review）注入矩阵摘要与降噪硬约束的单元测试。
"""

from __future__ import annotations

from esg_intelligence_agent import ESGIntelligenceAgent


def _build_agent():
    """构造带 config 的 agent 实例（不触发 LLM）。"""
    return ESGIntelligenceAgent()


def test_matrix_summary_includes_companies_topics_and_sources():
    agent = _build_agent()
    summary = agent._build_matrix_summary()

    assert "【目标监控企业】" in summary
    assert "华友钴业" in summary
    assert "【日常主题关键词】" in summary
    assert "【周报主题关键词】" in summary
    assert "【静态雷达轨道】" in summary
    # 现有矩阵已覆盖劳工权益类关键词，摘要中必须体现，供 LLM 去重参考
    assert "collective bargaining" in summary


def test_review_injects_matrix_summary_into_user_message(monkeypatch):
    agent = _build_agent()
    captured = {}

    def fake_llm(system_prompt, user_message, **kwargs):
        captured["system"] = system_prompt
        captured["user"] = user_message
        return "1. **缺失实体**:\n本周未发现明显盲区"

    monkeypatch.setattr(agent, "_call_llm_cheap", fake_llm)

    events = [
        {
            "entity": "特斯拉",
            "core_event_title_en": "Tesla wins IF Metall dispute",
            "risk_category": "合规与运营危机",
            "is_valid_risk": True,
            "is_direct_material_impact": False,
        }
    ]
    result = agent._weekly_threat_landscape_review(events)

    assert result == "1. **缺失实体**:\n本周未发现明显盲区"
    # 用户消息必须包含矩阵摘要
    assert "【目标监控企业】" in captured["user"]
    assert "【静态雷达轨道】" in captured["user"]
    # System prompt 必须包含相关性降噪与语言质量硬约束
    assert "有效:False" in captured["system"]
    assert "语言质量硬约束" in captured["system"]
    assert "相关性（高/中/低）" in captured["system"]


def test_review_returns_placeholder_on_no_events():
    agent = _build_agent()
    assert "无法进行态势审查" in agent._weekly_threat_landscape_review([])

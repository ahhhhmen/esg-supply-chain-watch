"""
Practice 轨道渲染方法（_generate_practice_report_and_filter）回归测试。

覆盖历史重构曾造成的方法体嵌套丢失（死代码 + AttributeError）回归，
以及无效实践过滤、分组、垃圾来源 URL 拦截行为。
"""

from __future__ import annotations

from pathlib import Path

from esg_intelligence_agent import ESGIntelligenceAgent


def _event(**overrides):
    event = {
        "entity": "宁德时代",
        "core_event_title_en": "CATL green factory best practice",
        "display_title_zh": "宁德时代绿色工厂实践",
        "learning_insight": "降低能耗 20%",
        "practice_category": "绿色制造与减碳",
        "date": "2026-08-10",
        "is_valid_practice": True,
        "is_replicable": True,
        "sources": [{"name": "Reuters", "url": "https://www.reuters.com/a"}],
    }
    event.update(overrides)
    return event


def test_practice_render_method_exists_and_reports(tmp_path):
    """方法必须存在且可用（曾因重构丢失导致 AttributeError）。"""
    events = [_event()]
    compat, valid = ESGIntelligenceAgent._generate_practice_report_and_filter(
        events, "practice", str(tmp_path / "practice.md")
    )
    assert len(valid) == 1
    assert len(compat) == 1


def test_practice_filter_drops_invalid_and_keeps_valid(tmp_path):
    events = [
        _event(entity="宁德时代", is_valid_practice=True),
        _event(entity="比亚迪", display_title_zh="比亚迪无效事件", is_valid_practice=False),
    ]
    _, valid = ESGIntelligenceAgent._generate_practice_report_and_filter(
        events, "practice", str(tmp_path / "practice.md")
    )
    assert [e["entity"] for e in valid] == ["宁德时代"]

    text = (tmp_path / "practice.md").read_text(encoding="utf-8")
    assert "宁德时代绿色工厂实践" in text
    assert "比亚迪无效事件" not in text


def test_practice_report_drops_junk_source_url(tmp_path):
    events = [
        _event(sources=[
            {"name": "Reuters", "url": "https://www.reuters.com/a"},
            {"name": "Analytics", "url": "https://www.google-analytics.com/analytics.js"},
        ])
    ]
    _, _ = ESGIntelligenceAgent._generate_practice_report_and_filter(
        events, "practice", str(tmp_path / "practice.md")
    )
    text = (tmp_path / "practice.md").read_text(encoding="utf-8")
    # 合法来源保留为链接
    assert "[Reuters](https://www.reuters.com/a)" in text
    # 垃圾 URL 不渲染为链接，媒体名保留为纯文本
    assert "Analytics" in text
    assert "google-analytics.com" not in text


def test_practice_report_header_and_grouping(tmp_path):
    events = [
        _event(practice_category="绿色制造与减碳"),
        _event(entity="比亚迪", display_title_zh="比亚迪回收实践", practice_category="循环经济与回收"),
    ]
    _, valid = ESGIntelligenceAgent._generate_practice_report_and_filter(
        events, "practice", str(tmp_path / "practice.md")
    )
    text = (tmp_path / "practice.md").read_text(encoding="utf-8")
    assert "同业良好实践周报" in text
    assert "绿色制造与减碳" in text
    assert "循环经济与回收" in text
    assert len(valid) == 2

"""
渲染层 source URL 兜底校验：拦截 LLM 从正文重新引入的垃圾直链
（统计脚本 / 静态资产 / 非新闻域名），确保不会进入 Markdown 报告或钉钉推送。
"""

from __future__ import annotations

from esg_intelligence_agent import ESGIntelligenceAgent


def _event_with_sources(sources):
    return {
        "entity": "通用汽车",
        "core_event_title_en": "GM test event",
        "display_title_zh": "通用汽车测试事件",
        "executive_insight": "测试洞察",
        "risk_category": "合规与运营危机",
        "date": "2026-08-13",
        "is_valid_risk": True,
        "is_direct_material_impact": False,
        "sources": sources,
    }


def test_is_clean_source_url_rejects_junk_domains():
    assert ESGIntelligenceAgent._is_clean_source_url("https://www.reuters.com/a") is True
    assert ESGIntelligenceAgent._is_clean_source_url("https://www.google-analytics.com/analytics.js") is False
    assert ESGIntelligenceAgent._is_clean_source_url("https://www.googletagmanager.com/gtm.js") is False
    assert ESGIntelligenceAgent._is_clean_source_url("https://fonts.googleapis.com/css?family=X") is False
    assert ESGIntelligenceAgent._is_clean_source_url("https://example.com/app.css") is False
    assert ESGIntelligenceAgent._is_clean_source_url("") is False
    assert ESGIntelligenceAgent._is_clean_source_url("not-a-url") is False


def test_dingtalk_sources_drop_junk_link_but_keep_name():
    srcs = [
        {"name": "Reuters", "url": "https://www.reuters.com/a"},
        {"name": "Analytics", "url": "https://www.google-analytics.com/analytics.js"},
    ]
    out, count = ESGIntelligenceAgent._format_sources_for_dingtalk(srcs, "en-US")

    # 合法来源保留为链接
    assert "[Reuters" in out
    assert "reuters.com/a" in out
    # 垃圾 URL 不渲染为链接，但媒体名保留
    assert "Analytics" in out
    assert "google-analytics.com" not in out
    assert count == 2


def test_markdown_watch_report_drops_junk_link(tmp_path):
    event = _event_with_sources([
        {"name": "Analytics", "url": "https://www.google-analytics.com/analytics.js"},
    ])
    report = tmp_path / "daily.md"

    valid_events, watch_events = ESGIntelligenceAgent._generate_v10_report_and_filter(
        [event], mode="daily", report_path=str(report)
    )

    text = report.read_text(encoding="utf-8")
    # 媒体名保留，垃圾 URL 不得以链接形式出现
    assert "Analytics" in text
    assert "google-analytics.com/analytics.js" not in text
    assert len(watch_events) == 1

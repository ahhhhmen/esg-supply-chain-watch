"""
钉钉 Notion 数据库链接构造 与 静态轨道时间窗覆盖 的单元测试。
"""

from __future__ import annotations

import esg_intelligence_agent as agent_mod
from esg_intelligence_agent import ESGIntelligenceAgent
from sourcing_engine import SourcingEngine


def test_dingtalk_notion_link_prefers_full_url(monkeypatch):
    agent = ESGIntelligenceAgent()
    agent._last_valid_events = []  # 走「无风险」心跳分支，构造最简 ding_content
    captured = {}

    def fake_send(webhook_url=None, markdown_text="", title="", secret=None):
        captured["markdown"] = markdown_text
        return True

    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://example.com/hook")
    monkeypatch.setenv("NOTION_DATABASE_ID", "db123")
    monkeypatch.setenv("NOTION_DATABASE_URL", "https://www.notion.so/team/abc")
    monkeypatch.setattr(agent_mod, "send_dingtalk", fake_send)

    agent.push_to_dingtalk(mode="daily")

    assert "📋 [查看完整数据库](https://www.notion.so/team/abc)" in captured["markdown"]
    # 不再使用硬编码 workspace slug 或 /p/ 路径
    assert "fangxie" not in captured["markdown"]


def test_dingtalk_notion_link_falls_back_to_id_shortlink(monkeypatch):
    agent = ESGIntelligenceAgent()
    agent._last_valid_events = []
    captured = {}

    def fake_send(webhook_url=None, markdown_text="", title="", secret=None):
        captured["markdown"] = markdown_text
        return True

    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://example.com/hook")
    monkeypatch.setenv("NOTION_DATABASE_ID", "db123")
    monkeypatch.delenv("NOTION_DATABASE_URL", raising=False)
    monkeypatch.setattr(agent_mod, "send_dingtalk", fake_send)

    agent.push_to_dingtalk(mode="daily")

    assert "📋 [查看完整数据库](https://www.notion.so/db123)" in captured["markdown"]


def test_static_sources_time_window_override(monkeypatch):
    engine = SourcingEngine()
    seen_overrides = []

    def fake_fetch_google_news_rss(source, time_window_override=None):
        seen_overrides.append(time_window_override)
        return []

    monkeypatch.setattr(engine, "_fetch_google_news_rss", fake_fetch_google_news_rss)
    monkeypatch.setattr(engine, "_fetch_html_target", lambda source: [])

    engine.fetch_all_active_sources(time_window_override="7d")

    # 所有 google_news_rss 型轨道都应收到 "7d" 覆盖
    assert seen_overrides, "没有任何 RSS 轨道被调用"
    assert all(o == "7d" for o in seen_overrides)

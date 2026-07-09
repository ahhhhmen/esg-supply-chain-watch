"""Tests for DingTalk risk-push deduplication, materiality, and formatting."""

import sys
import types


class _TokenUsage:
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    total_cost_usd = 0.0

    def __add__(self, other):
        return self

    def summary(self):
        return "in=0 out=0 total=0 cost=$0.0000"


radar_infra = types.ModuleType("radar_infra")
radar_infra_llm = types.ModuleType("radar_infra.llm")
radar_infra_fetch = types.ModuleType("radar_infra.fetch")
radar_infra_guard = types.ModuleType("radar_infra.guard")
radar_infra_sink = types.ModuleType("radar_infra.sink")
radar_infra_sink_dingtalk = types.ModuleType("radar_infra.sink.dingtalk")

radar_infra_llm.create_provider = lambda: None
radar_infra_llm.create_cheap_provider = lambda: None
radar_infra_llm.BaseLLMProvider = object
radar_infra_llm.TokenUsage = _TokenUsage
radar_infra_llm.create_llm_retry_decorator = lambda max_attempts=1: (lambda fn: fn)

radar_infra_fetch.extract_article_body = lambda url, min_length=0, max_length=0: "mocked_body"

import json
def _safe_json_parse(text):
    try:
        return json.loads(text)
    except Exception:
        import re
        m = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
        return None
radar_infra_guard.safe_json_parse = _safe_json_parse

import hashlib
def _generate_external_id(*parts):
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
radar_infra_sink.generate_external_id = _generate_external_id

radar_infra_sink_dingtalk.send_dingtalk = lambda *a, **k: True

sys.modules.setdefault("radar_infra", radar_infra)
sys.modules.setdefault("radar_infra.llm", radar_infra_llm)
sys.modules.setdefault("radar_infra.fetch", radar_infra_fetch)
sys.modules.setdefault("radar_infra.guard", radar_infra_guard)
sys.modules.setdefault("radar_infra.sink", radar_infra_sink)
sys.modules.setdefault("radar_infra.sink.dingtalk", radar_infra_sink_dingtalk)

from esg_intelligence_agent import ESGIntelligenceAgent


def _event(**overrides):
    base = {
        "entity": "大众汽车",
        "core_event_title_en": "Volkswagen plans major layoffs",
        "display_title_zh": "大众汽车计划裁员10万人",
        "original_language": "英语",
        "executive_insight": "大众汽车宣布裁员计划，需关注其电动车生产节奏。",
        "date": "2026-06-28",
        "sources": [{"name": "Ad-hoc-news.de", "url": "https://ad-hoc-news.de/vw-layoffs"}],
        "risk_category": "合规与运营危机",
        "is_valid_risk": True,
        "is_direct_material_impact": True,
    }
    base.update(overrides)
    return base


class TestDingTalkDeduplication:
    def test_volkswagen_layoff_variants_merge(self):
        events = [
            _event(),
            _event(
                core_event_title_en="Volkswagen to close German plants and cut 100000 jobs",
                display_title_zh="大众汽车拟关闭4家德国工厂并全球裁员10万人，德国工会反对",
                sources=[
                    {"name": "mezha.net", "url": "https://mezha.net/vw-germany-plants"},
                    {"name": "新浪网", "url": "https://finance.sina.com.cn/vw"},
                ],
            ),
        ]

        merged = ESGIntelligenceAgent._merge_same_company_events(events)

        assert len(merged) == 1
        assert "关闭4家德国工厂" in merged[0]["display_title_zh"]
        assert len(merged[0]["sources"]) == 3

    def test_different_same_entity_events_do_not_merge(self):
        events = [
            _event(),
            _event(
                core_event_title_en="Volkswagen recalls vehicles over software defect",
                display_title_zh="大众汽车因软件缺陷召回部分车型",
                executive_insight="大众汽车因软件缺陷召回部分车型，事件未指向材料端。",
                sources=[{"name": "Reuters", "url": "https://reuters.com/vw-recall"}],
            ),
        ]

        merged = ESGIntelligenceAgent._merge_same_company_events(events)

        assert len(merged) == 2

    def test_tesla_sweden_strike_scale_back_variants_merge(self):
        events = [
            _event(
                entity="特斯拉",
                core_event_title_en="Tesla Sweden strike action scaled back",
                display_title_zh="特斯拉在瑞典的长期罢工被工会缩减规模",
                date="2026-05-29",
                sources=[{"name": "Source A", "url": "https://example.com/a"}],
            ),
            _event(
                entity="特斯拉",
                core_event_title_en="IF Metall partially scales back strike against Tesla in Sweden",
                display_title_zh="IF Metall部分缩减在瑞典针对特斯拉的罢工行动",
                date="2026-05-29",
                sources=[{"name": "Source B", "url": "https://example.com/b"}],
            ),
            _event(
                entity="特斯拉",
                core_event_title_en="Tesla Sweden strike scaled back as mechanics return",
                display_title_zh="特斯拉瑞典罢工规模缩减，IF Metall要求机械师复工",
                date="2026-05-29",
                sources=[{"name": "Source C", "url": "https://example.com/c"}],
            ),
        ]

        merged = ESGIntelligenceAgent._merge_same_company_events(events)

        assert len(merged) == 1
        assert len(merged[0]["sources"]) == 3

    def test_dingtalk_selection_dedupes_across_materiality_tiers(self):
        events = [
            _event(
                entity="宝马",
                core_event_title_en="BMW engine fire leads to Korea sales ban",
                display_title_zh="宝马发动机起火导致韩国禁令",
                date="2026-05-23",
                materiality="🔴 直接材料冲击",
                is_direct_material_impact=True,
                materiality_basis="公开信息指向材料端直接传导",
                sources=[{"name": "A", "url": "https://example.com/a"}],
            ),
            _event(
                entity="宝马",
                core_event_title_en="BMW engine fire causes South Korea sales ban",
                display_title_zh="宝马发动机起火导致韩国禁售",
                date="2026-05-23",
                materiality="🟡 战略观察",
                is_direct_material_impact=False,
                materiality_basis="传导链暂未触及上游电池材料端",
                sources=[{"name": "B", "url": "https://second.example/b"}],
            ),
        ]

        content = ESGIntelligenceAgent._format_for_dingtalk(
            events,
            "daily",
            "2026-05-23 09:00",
            "2026-05-23",
            "2026-05-23",
        )

        assert "🔴 1 条直接材料冲击 · 🟡 0 条战略观察" in content
        assert content.count("宝马发动机起火导致韩国") == 1
        assert "2 家去重来源" in content


class TestMaterialityGuardrails:
    def test_oem_restructuring_without_material_trigger_becomes_watch(self, tmp_path):
        report = tmp_path / "daily.md"
        _, valid_events = ESGIntelligenceAgent._generate_v10_report_and_filter(
            [_event()], "daily", str(report)
        )

        assert len(valid_events) == 1
        assert valid_events[0]["is_direct_material_impact"] is False
        assert valid_events[0]["materiality"] == "🟡 战略观察"
        assert "未确认电动车" in valid_events[0]["materiality_basis"]
        assert "需关注" not in valid_events[0]["executive_insight"]

    def test_battery_plant_shutdown_remains_material(self, tmp_path):
        report = tmp_path / "daily.md"
        event = _event(
            core_event_title_en="Volkswagen battery plant shutdown halts EV production",
            display_title_zh="大众汽车电池工厂停产导致电动车生产暂停",
            executive_insight="大众汽车电池工厂停产导致电动车生产暂停，已触及电池材料需求传导。",
        )

        _, valid_events = ESGIntelligenceAgent._generate_v10_report_and_filter(
            [event], "daily", str(report)
        )

        assert len(valid_events) == 1
        assert valid_events[0]["is_direct_material_impact"] is True
        assert valid_events[0]["materiality"] == "🔴 直接材料冲击"


class TestDingTalkFormatting:
    def test_header_separates_material_and_watch_counts(self):
        content = ESGIntelligenceAgent._format_for_dingtalk(
            [
                _event(
                    is_direct_material_impact=False,
                    materiality="🟡 战略观察",
                    materiality_basis="未确认电动车、电池工厂或材料订单直接受影响",
                    executive_insight="大众汽车裁员计划反映成本压力，当前未确认材料订单削减。",
                )
            ],
            "daily",
            "2026-06-28 23:13",
            "2026-06-28",
            "2026-06-28",
        )

        assert "🔴 0 条直接材料冲击 · 🟡 1 条战略观察 · 1 家企业" in content
        assert "材料冲击 · 1 家企业" not in content
        assert "**判定依据**：未确认电动车、电池工厂或材料订单直接受影响" in content

    def test_banned_advisory_phrases_do_not_render_after_guardrail(self, tmp_path):
        _, valid_events = ESGIntelligenceAgent._generate_v10_report_and_filter(
            [_event()], "daily", str(tmp_path / "daily.md")
        )
        content = ESGIntelligenceAgent._format_for_dingtalk(
            valid_events,
            "daily",
            "2026-06-28 23:13",
            "2026-06-28",
            "2026-06-28",
        )

        assert "需关注" not in content
        assert "建议" not in content
        assert "应当" not in content
        assert "可能影响运营" not in content

    def test_source_folding_uses_unique_sources(self):
        event = _event(
            is_direct_material_impact=True,
            materiality="🔴 直接材料冲击",
            materiality_basis="公开信息指向电池/材料订单、生产或准入限制",
            executive_insight="大众汽车电池工厂停产导致电动车生产暂停，已触及电池材料需求传导。",
            sources=[
                {"name": "Reuters", "url": "https://reuters.com/vw-battery"},
                {"name": "Reuters Copy", "url": "https://reuters.com/vw-battery?utm=1"},
                {"name": "Bloomberg", "url": "https://bloomberg.com/vw-battery"},
                {"name": "新浪网", "url": "https://finance.sina.com.cn/vw-battery"},
            ],
        )

        content = ESGIntelligenceAgent._format_for_dingtalk(
            [event],
            "daily",
            "2026-06-28 23:13",
            "2026-06-28",
            "2026-06-28",
        )

        assert "3 家去重来源" in content
        assert "等 3 家" in content


class TestLlmGlobalConvergenceInvocation:
    def test_llm_global_convergence_invoked_when_agent_provided(self, tmp_path):
        from unittest.mock import MagicMock
        mock_agent = MagicMock()
        mock_agent._llm_global_convergence.return_value = [{"event": "mocked"}]
        
        # Create two material events so that len(valid_events) > 1
        event1 = _event(
            entity="大众汽车",
            core_event_title_en="Volkswagen battery plant shutdown halts EV production 1",
            display_title_zh="大众汽车电池工厂停产导致电动车生产暂停 1",
            executive_insight="大众汽车电池工厂停产导致电动车生产暂停，已触及电池材料需求传导 1",
        )
        event2 = _event(
            entity="特斯拉",
            core_event_title_en="Tesla battery plant shutdown halts EV production 2",
            display_title_zh="特斯拉电池工厂停产导致电动车生产暂停 2",
            executive_insight="特斯拉电池工厂停产导致电动车生产暂停，已触及电池材料需求传导 2",
        )
        
        report = tmp_path / "daily.md"
        _, valid_events = ESGIntelligenceAgent._generate_v10_report_and_filter(
            [event1, event2], "daily", str(report), agent=mock_agent
        )
        
        # Verify that mock_agent._llm_global_convergence was called
        mock_agent._llm_global_convergence.assert_called_once()
        # Verify it returned the mocked value
        assert len(valid_events) == 1
        assert valid_events[0] == {"event": "mocked"}

    def test_llm_global_convergence_skipped_when_agent_none(self, tmp_path):
        event1 = _event(
            entity="大众汽车",
            core_event_title_en="Volkswagen battery plant shutdown halts EV production 1",
            display_title_zh="大众汽车电池工厂停产导致电动车生产暂停 1",
            executive_insight="大众汽车电池工厂停产导致电动车生产暂停，已触及电池材料需求传导 1",
        )
        event2 = _event(
            entity="特斯拉",
            core_event_title_en="Tesla battery plant shutdown halts EV production 2",
            display_title_zh="特斯拉电池工厂停产导致电动车生产暂停 2",
            executive_insight="特斯拉电池工厂停产导致电动车生产暂停，已触及电池材料需求传导 2",
        )
        
        report = tmp_path / "daily.md"
        _, valid_events = ESGIntelligenceAgent._generate_v10_report_and_filter(
            [event1, event2], "daily", str(report), agent=None
        )
        
        # It should run fine without crashing and retain both events
        assert len(valid_events) == 2


class TestUrlPrefixMapping:
    def test_google_news_url_prefix_restoration(self):
        truncated_url = "https://news.google.com/rss/articles/CBMilgFBVV95cUxOTHpMdGRuQUlyUEctYml3aE5ST0wxMUxlMkIwSmlvTnhSNkRPb2dzN"
        full_url = "https://news.google.com/rss/articles/CBMilgFBVV95cUxOTHpMdGRuQUlyUEctYml3aE5ST0wxMUxlMkIwSmlvTnhSNkRPb2dzNHFpNllFMk5ZbVlpTi0yUU9RZFhEZWs5dTNTY3BLaTBHSG56S3NfbUZZdVRiNXFFQW56NjVjMXJNSWVHSF9UWHkwVm1iUzh4ck1hTEtubTJKbUZ2cWVWZjVxSFlVZXNaTkl4TF9iOFE?oc=5"
        
        fallback_sources = [
            {"name": "BHRRC", "url": full_url}
        ]
        
        # Test 1: Event with sources having truncated URL
        event = {
            "entity": "华友钴业",
            "sources": [
                {"name": "BHRRC", "url": truncated_url}
            ]
        }
        
        resolved = ESGIntelligenceAgent._attach_source_urls(event, fallback_sources)
        assert resolved["sources"][0]["url"] == full_url

        # Test 2: Event with source_urls having truncated URL and empty sources urls
        event2 = {
            "entity": "华友钴业",
            "source_urls": [truncated_url],
            "sources": [
                {"name": "BHRRC", "url": ""}
            ]
        }
        resolved2 = ESGIntelligenceAgent._attach_source_urls(event2, fallback_sources)
        assert resolved2["sources"][0]["url"] == full_url
        assert resolved2["source_urls"][0] == full_url


class TestWeeklyReportDingtalkStructure:
    def test_weekly_review_isolated_from_dingtalk(self):
        events = [
            _event(
                entity="华友钴业",
                core_event_title_en="Weekly event",
                display_title_zh="周报测试事件",
                materiality="🟡 战略观察",
                is_direct_material_impact=False,
            )
        ]
        
        review_text = "1. **缺失实体**: Jervois Global\n2. **缺失关键词**: 镍精炼\n3. **新兴威胁模式**: 巴西镍供应链\n4. **具体建议**: YAML configuration"
        
        content = ESGIntelligenceAgent._format_for_dingtalk(
            events,
            "weekly",
            "2026-07-02 23:14",
            "2026-06-24",
            "2026-07-01",
            weekly_review_text=review_text,
        )
        
        # Check that directory and body do NOT contain weekly review or references to it
        assert "- 🔍 **监控矩阵盲区分析**" not in content
        assert "## 🔍 监控矩阵盲区分析" not in content
        assert "巴西镍供应链" not in content
        
        # Check that strategic observations are still present
        assert "## 📡 战略观察清单" in content





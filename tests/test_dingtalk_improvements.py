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
radar_infra_llm.create_provider = lambda: None
radar_infra_llm.create_cheap_provider = lambda: None
radar_infra_llm.BaseLLMProvider = object
radar_infra_llm.TokenUsage = _TokenUsage
radar_infra_llm.create_llm_retry_decorator = lambda max_attempts=1: (lambda fn: fn)
sys.modules.setdefault("radar_infra", radar_infra)
sys.modules.setdefault("radar_infra.llm", radar_infra_llm)

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
        assert "判定依据：未确认电动车、电池工厂或材料订单直接受影响" in content

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

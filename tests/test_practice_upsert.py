"""Unit tests for practice 轨道 — map_practice_category / build_practice_properties / upsert_practice_page."""

from unittest.mock import MagicMock

import pytest

from notion_upsert import (
    generate_external_id,
    build_practice_properties,
    upsert_practice_page,
)
from notion_mapping import EXTERNAL_ID, map_practice_category


# ── sample practice event fixture ─────────────────────────

@pytest.fixture
def sample_practice_event():
    return {
        "entity": "比亚迪",
        "date": "2026-06-18",
        "display_title_zh": "比亚迪宣布全球工厂实现100%绿电覆盖",
        "english_title": "BYD achieves 100% renewable electricity across global plants",
        "practice_category": "绿色制造与减碳",
        "learning_insight": "华友可借鉴其绿电采购与屋顶光伏组合策略，降低冶炼环节碳足迹。",
        "is_replicable": True,
        "sources": "Reuters, Bloomberg",
        "mode": "practice",
        "push_date": "2026-06-18",
    }


# ── map_practice_category tests ───────────────────────────

class TestMapPracticeCategory:
    def test_exact_match(self):
        """标准分类名原样返回。"""
        assert map_practice_category("绿色制造与减碳") == "绿色制造与减碳"
        assert map_practice_category("循环经济与回收") == "循环经济与回收"
        assert map_practice_category("技术创新与工艺升级") == "技术创新与工艺升级"

    def test_alias_match(self):
        """常见别名映射到标准分类。"""
        assert map_practice_category("绿色制造") == "绿色制造与减碳"
        assert map_practice_category("电池回收") == "循环经济与回收"
        assert map_practice_category("技术创新") == "技术创新与工艺升级"
        assert map_practice_category("负责任采购") == "供应链尽职调查与合规标杆"

    def test_fuzzy_match(self):
        """包含关键词的模糊匹配。"""
        assert map_practice_category("ESG报告披露最佳实践") == "ESG披露与治理"
        assert map_practice_category("碳中和路线图") == "绿色制造与减碳"

    def test_empty_returns_default(self):
        """空输入兜底为 ESG披露与治理。"""
        assert map_practice_category("") == "ESG披露与治理"
        assert map_practice_category(None) == "ESG披露与治理"

    def test_unknown_returns_default(self):
        """完全未知分类兜底为 ESG披露与治理。"""
        assert map_practice_category("火星殖民") == "ESG披露与治理"


# ── build_practice_properties tests ───────────────────────

class TestBuildPracticeProperties:
    def test_includes_all_required_fields(self, sample_practice_event):
        sample_practice_event["_external_id"] = "abc123def456"
        props = build_practice_properties(sample_practice_event)
        # 与风险库共有的字段
        assert "标题" in props
        assert "English Title" in props
        assert "Entity" in props
        assert "Date" in props
        assert "Sources" in props
        assert "Mode" in props
        assert "Push Date" in props
        assert EXTERNAL_ID in props
        # practice 独有字段
        assert "Practice Category" in props
        assert "Learning Insight" in props
        assert "Replicable" in props
        # 风险库独有字段不应出现
        assert "Risk Category" not in props
        assert "Executive Insight" not in props

    def test_includes_external_id(self, sample_practice_event):
        sample_practice_event["_external_id"] = "xyz789abc012"
        props = build_practice_properties(sample_practice_event)
        ext_id_content = props[EXTERNAL_ID]["rich_text"][0]["text"]["content"]
        assert ext_id_content == "xyz789abc012"

    def test_replicable_checkbox(self, sample_practice_event):
        sample_practice_event["_external_id"] = "abc123def456"
        sample_practice_event["is_replicable"] = True
        props = build_practice_properties(sample_practice_event)
        assert props["Replicable"]["checkbox"] is True

        sample_practice_event["is_replicable"] = False
        props = build_practice_properties(sample_practice_event)
        assert props["Replicable"]["checkbox"] is False

    def test_practice_category_mapping_applied(self, sample_practice_event):
        """验证分类映射通过 map_practice_category 生效。"""
        sample_practice_event["_external_id"] = "abc123def456"
        sample_practice_event["practice_category"] = "碳中和"  # 别名
        props = build_practice_properties(sample_practice_event)
        assert props["Practice Category"]["select"]["name"] == "绿色制造与减碳"

    def test_mode_defaults_to_practice(self, sample_practice_event):
        """mode 缺省时应填 practice。"""
        sample_practice_event["_external_id"] = "abc123def456"
        del sample_practice_event["mode"]
        props = build_practice_properties(sample_practice_event)
        assert props["Mode"]["select"]["name"] == "practice"

    def test_truncates_long_fields(self):
        event = {
            "entity": "比亚迪",
            "date": "2026-06-18",
            "display_title_zh": "B" * 2500,
            "english_title": "C" * 2500,
            "learning_insight": "D" * 2500,
            "sources": "E" * 2500,
            "practice_category": "绿色制造与减碳",
            "mode": "practice",
            "push_date": "2026-06-18",
            "_external_id": "test12345678",
        }
        props = build_practice_properties(event)
        title_content = props["标题"]["title"][0]["text"]["content"]
        assert len(title_content) <= 2000


# ── upsert_practice_page tests ────────────────────────────

class TestUpsertPracticePage:
    def test_create_new_page(self, sample_practice_event):
        """首次写入实践事件应创建新页面。"""
        mock_notion = MagicMock()
        mock_notion.databases.retrieve.return_value = {"data_sources": []}
        mock_notion.databases.query.return_value = {"results": []}
        mock_notion.pages.create.return_value = {"id": "practice-page-1"}

        action, page_id = upsert_practice_page(
            sample_practice_event, mock_notion, "practice-db", dry_run=False,
        )

        assert action == "created"
        assert page_id == "practice-page-1"
        mock_notion.pages.create.assert_called_once()

    def test_update_existing_page(self, sample_practice_event):
        """重复事件应更新已有页面。"""
        mock_notion = MagicMock()
        mock_notion.databases.retrieve.return_value = {"data_sources": []}
        mock_notion.databases.query.return_value = {
            "results": [{"id": "practice-existing"}]
        }

        action, page_id = upsert_practice_page(
            sample_practice_event, mock_notion, "practice-db", dry_run=False,
        )

        assert action == "updated"
        assert page_id == "practice-existing"
        mock_notion.pages.update.assert_called_once()
        mock_notion.pages.create.assert_not_called()

    def test_dry_run_no_api_calls(self, sample_practice_event):
        """干跑模式不应调用任何 API。"""
        mock_notion = MagicMock()
        action, page_id = upsert_practice_page(
            sample_practice_event, mock_notion, "practice-db", dry_run=True,
        )
        assert action == "skipped"
        assert page_id is None
        mock_notion.pages.create.assert_not_called()
        mock_notion.pages.update.assert_not_called()


# ── External ID 幂等性跨库验证 ────────────────────────────

class TestPracticeIdempotency:
    def test_external_id_same_for_practice_and_risk(self):
        """相同 entity/date/title 在风险库和实践库应产生相同 External ID，
        因为幂等键与 schema 无关。"""
        eid = generate_external_id("比亚迪", "2026-06-18", "绿电覆盖")
        assert len(eid) == 12

    def test_idempotent_same_practice_twice(self, sample_practice_event):
        """同一实践事件写入两次：第一次创建，第二次更新。"""
        mock_notion = MagicMock()
        mock_notion.databases.retrieve.return_value = {"data_sources": []}
        mock_notion.databases.query.return_value = {"results": []}
        mock_notion.pages.create.return_value = {"id": "page-new"}

        action1, _ = upsert_practice_page(sample_practice_event, mock_notion, "practice-db")
        assert action1 == "created"

        mock_notion.reset_mock()
        mock_notion.databases.retrieve.return_value = {"data_sources": []}
        mock_notion.databases.query.return_value = {
            "results": [{"id": "page-existing"}]
        }

        action2, _ = upsert_practice_page(sample_practice_event, mock_notion, "practice-db")
        assert action2 == "updated"

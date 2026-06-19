"""Unit tests for notion_upsert — idempotent Notion write helper."""

from unittest.mock import MagicMock, patch

import pytest

from notion_upsert import (
    generate_external_id,
    query_page_by_external_id,
    upsert_notion_page,
    build_notion_properties,
)
from notion_mapping import EXTERNAL_ID


# ── sample event fixture ──────────────────────────────────

@pytest.fixture
def sample_event():
    return {
        "entity": "大众汽车",
        "date": "2026-06-18",
        "display_title_zh": "大众汽车宣布在德国裁员1.9万人",
        "english_title": "Volkswagen announces 19,000 layoffs in Germany",
        "insight": "大众汽车在德国裁员以削减成本应对电动化转型。",
        "sources": "Reuters, Bloomberg",
        "category": "合规与运营危机",
        "mode": "daily",
        "push_date": "2026-06-18",
    }


# ── generate_external_id tests ────────────────────────────

class TestGenerateExternalId:
    def test_deterministic_same_input(self):
        """相同输入产生相同的 External ID。"""
        id1 = generate_external_id("大众", "2026-06-18", "裁员")
        id2 = generate_external_id("大众", "2026-06-18", "裁员")
        assert id1 == id2

    def test_different_inputs(self):
        """不同输入产生不同的 External ID。"""
        id1 = generate_external_id("大众", "2026-06-18", "裁员")
        id2 = generate_external_id("丰田", "2026-06-18", "裁员")
        assert id1 != id2

    def test_12_char_hex(self):
        """返回 12 位十六进制字符串。"""
        eid = generate_external_id("大众", "2026-06-18", "裁员")
        assert len(eid) == 12
        assert all(c in "0123456789abcdef" for c in eid)

    def test_empty_fields(self):
        """空字段也能正常生成 ID。"""
        eid = generate_external_id("", "", "")
        assert len(eid) == 12


# ── build_notion_properties tests ─────────────────────────

class TestBuildNotionProperties:
    def test_includes_external_id(self, sample_event):
        sample_event["_external_id"] = "abc123def456"
        props = build_notion_properties(sample_event)
        assert EXTERNAL_ID in props
        ext_id_content = props[EXTERNAL_ID]["rich_text"][0]["text"]["content"]
        assert ext_id_content == "abc123def456"

    def test_includes_all_required_fields(self, sample_event):
        sample_event["_external_id"] = "abc123def456"
        props = build_notion_properties(sample_event)
        assert "标题" in props
        assert "English Title" in props
        assert "Entity" in props
        assert "Risk Category" in props
        assert "Date" in props
        assert "Executive Insight" in props
        assert "Sources" in props
        assert "Mode" in props
        assert "Push Date" in props

    def test_truncates_long_fields(self):
        event = {
            "entity": "A" * 3000,
            "date": "2026-06-18",
            "display_title_zh": "B" * 2500,
            "english_title": "C" * 2500,
            "insight": "D" * 2500,
            "sources": "E" * 2500,
            "category": "供应链断裂预警",
            "mode": "daily",
            "push_date": "2026-06-18",
            "_external_id": "test12345678",
        }
        props = build_notion_properties(event)
        # 标题字段有 2000 字符限制
        title_content = props["标题"]["title"][0]["text"]["content"]
        assert len(title_content) <= 2000

    def test_category_mapping(self):
        """验证分类映射通过 map_risk_category 生效。"""
        event = {
            "entity": "测试",
            "date": "2026-06-18",
            "display_title_zh": "测试事件",
            "english_title": "",
            "insight": "罢工抗议",
            "sources": "",
            "category": "合规与运营危机",
            "mode": "daily",
            "push_date": "2026-06-18",
            "_external_id": "test12345678",
        }
        props = build_notion_properties(event)
        # 关键词"罢工"应映射为劳工权益
        assert props["Risk Category"]["select"]["name"] == "劳工权益"


# ── upsert_notion_page tests ──────────────────────────────

class TestUpsertNotionPage:
    def test_create_new_page(self, sample_event):
        """首次写入事件应创建新页面。"""
        mock_notion = MagicMock()
        mock_notion.databases.retrieve.return_value = {"data_sources": []}
        mock_notion.databases.query.return_value = {"results": []}
        mock_notion.pages.create.return_value = {"id": "page-123"}

        action, page_id = upsert_notion_page(
            sample_event, mock_notion, "db-123", dry_run=False,
        )

        assert action == "created"
        assert page_id == "page-123"
        mock_notion.pages.create.assert_called_once()

    def test_update_existing_page(self, sample_event):
        """重复事件应更新已有页面。"""
        mock_notion = MagicMock()
        mock_notion.databases.retrieve.return_value = {"data_sources": []}
        mock_notion.databases.query.return_value = {
            "results": [{"id": "page-existing"}]
        }

        action, page_id = upsert_notion_page(
            sample_event, mock_notion, "db-123", dry_run=False,
        )

        assert action == "updated"
        assert page_id == "page-existing"
        mock_notion.pages.update.assert_called_once()
        mock_notion.pages.create.assert_not_called()

    def test_dry_run_no_api_calls(self, sample_event):
        """干跑模式不应调用任何 API。"""
        mock_notion = MagicMock()

        action, page_id = upsert_notion_page(
            sample_event, mock_notion, "db-123", dry_run=True,
        )

        assert action == "skipped"
        assert page_id is None
        mock_notion.pages.create.assert_not_called()
        mock_notion.pages.update.assert_not_called()
        mock_notion.databases.query.assert_not_called()

    def test_idempotent_same_event_twice(self, sample_event):
        """同一事件写入两次：第一次创建，第二次更新。"""
        mock_notion = MagicMock()
        # 第一次：无匹配
        mock_notion.databases.retrieve.return_value = {"data_sources": []}
        mock_notion.databases.query.return_value = {"results": []}
        mock_notion.pages.create.return_value = {"id": "page-new"}

        action1, _ = upsert_notion_page(sample_event, mock_notion, "db-123")
        assert action1 == "created"

        # 第二次：已存在
        mock_notion.reset_mock()
        mock_notion.databases.retrieve.return_value = {"data_sources": []}
        mock_notion.databases.query.return_value = {
            "results": [{"id": "page-existing"}]
        }

        action2, _ = upsert_notion_page(sample_event, mock_notion, "db-123")
        assert action2 == "updated"

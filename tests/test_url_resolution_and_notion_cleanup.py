"""Tests for publisher URL resolution and Notion cleanup planning."""

from unittest.mock import MagicMock

import clean_notion
import notion_upsert
from esg_agent.canonical import (
    canonical_event_key,
    canonical_external_id,
    fallback_english_title,
    original_english_title,
    semantic_event_signature,
)
from esg_agent.fetchers import resolve_news_url


class _Resp:
    def __init__(self, url: str, text: str = ""):
        self.url = url
        self.text = text


def test_resolve_news_url_uses_query_param_without_network():
    url = "https://news.google.com/rss/articles/foo?url=https%3A%2F%2Fexample.com%2Farticle"
    assert resolve_news_url(url) == "https://example.com/article"


def test_resolve_news_url_decodes_google_news_locally():
    target_url = "https://www.business-humanrights.org/en/latest-news/global-mineral-tracker-2026"
    fake_protobuf = b"\x08\x01\x12\x4b" + target_url.encode('utf-8') + b"\x1a\x05stuff"
    import base64
    encoded = base64.urlsafe_b64encode(fake_protobuf).decode('utf-8').rstrip('=')
    google_url = f"https://news.google.com/rss/articles/{encoded}"
    resolved = resolve_news_url(google_url)
    assert resolved == target_url



def test_resolve_news_url_gets_original_from_html(monkeypatch):
    google_url = "https://news.google.com/rss/articles/example"
    original = "https://publisher.example.com/story"

    def fake_head(*args, **kwargs):
        return _Resp(google_url)

    def fake_get(*args, **kwargs):
        return _Resp(google_url, f'<html><head><link rel="canonical" href="{original}"></head></html>')

    monkeypatch.setattr("esg_agent.fetchers.requests.head", fake_head)
    monkeypatch.setattr("esg_agent.fetchers.requests.get", fake_get)

    assert resolve_news_url(google_url) == original


def test_build_notion_properties_resolves_source_links(monkeypatch):
    monkeypatch.setattr(
        notion_upsert,
        "resolve_news_url",
        lambda url: "https://publisher.example.com/story" if "news.google.com" in url else url,
    )

    props = notion_upsert.build_notion_properties({
        "entity": "大众汽车",
        "date": "2026-06-28",
        "display_title_zh": "大众汽车拟关闭德国工厂",
        "category": "合规与运营危机",
        "sources": [{"name": "Google News", "url": "https://news.google.com/rss/articles/x"}],
        "_external_id": "abc123def456",
    })

    sources = props["Sources"]["rich_text"]
    assert sources[0]["text"]["content"] == "Google News"
    assert sources[0]["text"]["link"]["url"] == "https://publisher.example.com/story"
    assert "news.google.com" not in str(sources)


def test_build_notion_properties_strips_source_suffixes():
    props = notion_upsert.build_notion_properties({
        "entity": "宝马",
        "date": "2026-05-23",
        "display_title_zh": "宝马发动机起火导致韩国禁售",
        "original_language": "英语",
        "sources": [
            {"name": "Discovery Alert (英语)", "url": "https://example.com/a"},
            {"name": "富途牛牛 (英语)", "url": "https://example.com/b"},
        ],
        "_external_id": "abc123def456",
    })

    sources = props["Sources"]["rich_text"]
    texts = [item["text"]["content"] for item in sources if "text" in item]
    assert "Discovery Alert (英语)" not in "".join(texts)
    assert "富途牛牛 (英语)" not in "".join(texts)
    assert "Discovery Alert" in "".join(texts)
    assert "富途牛牛" in "".join(texts)


def test_clean_notion_plans_duplicate_archive_and_external_id_backfill():
    page1 = clean_notion.PageRecord(
        page_id="keep",
        title="大众汽车拟关闭4家德国工厂并全球裁员10万人，德国工会反对",
        entity="大众汽车",
        date="2026-06-28",
        sources="[Reuters](https://reuters.com/vw)",
        english_title="",
        external_id="",
        materiality="🟡 战略观察",
        last_edited_time="2026-06-29T00:00:00.000Z",
        raw={},
    )
    page2 = clean_notion.PageRecord(
        page_id="archive",
        title="大众汽车计划裁员10万人",
        entity="大众汽车",
        date="2026-06-28",
        sources="[Ad-hoc](https://adhoc.example/vw)",
        english_title="",
        external_id="",
        materiality="🟡 战略观察",
        last_edited_time="2026-06-28T00:00:00.000Z",
        raw={},
    )

    assert clean_notion._dedupe_key(page1) == clean_notion._dedupe_key(page2)
    assert clean_notion._choose_canonical([page1, page2]).page_id == "keep"


def test_clean_notion_ignores_stale_external_id_when_grouping_duplicates():
    page1 = clean_notion.PageRecord(
        page_id="vw-1",
        title="大众汽车拟关闭4家德国工厂并全球裁员10万人，德国工会反对",
        entity="大众汽车",
        date="2026-06-28",
        sources="",
        english_title="",
        external_id="old-title-hash-1",
        materiality="🟡 战略观察",
        last_edited_time="2026-06-29T00:00:00.000Z",
        raw={},
    )
    page2 = clean_notion.PageRecord(
        page_id="vw-2",
        title="大众汽车计划裁员10万人",
        entity="大众汽车",
        date="2026-06-28",
        sources="",
        english_title="",
        external_id="old-title-hash-2",
        materiality="🟡 战略观察",
        last_edited_time="2026-06-28T00:00:00.000Z",
        raw={},
    )

    assert clean_notion._dedupe_key(page1) == clean_notion._dedupe_key(page2)
    assert clean_notion._dedupe_key(page1) != f"external:{page1.external_id}"


def test_canonical_event_key_merges_named_duplicate_families():
    mercedes_a = {
        "entity": "梅赛德斯-奔驰",
        "date": "2026-06-02",
        "display_title_zh": "梅赛德斯-奔驰因中国股权问题面临美国销售禁令威胁",
    }
    mercedes_b = {
        "entity": "Mercedes-Benz",
        "date": "2026-06-05",
        "display_title_zh": "美国法案或因中国持股禁止梅赛德斯-奔驰在美销售",
    }
    gm_a = {
        "entity": "通用汽车",
        "date": "2026-06-02",
        "display_title_zh": "UAW在通用汽车关键卡车供应商工厂举行罢工",
    }
    gm_b = {
        "entity": "General Motors",
        "date": "2026-06-03",
        "display_title_zh": "UAW在通用汽车车轴工厂罢工，威胁卡车生产",
    }

    assert canonical_event_key(mercedes_a) == canonical_event_key(mercedes_b)
    assert canonical_event_key(gm_a) == canonical_event_key(gm_b)
    assert canonical_external_id(mercedes_a) == canonical_external_id(mercedes_b)


def test_canonical_event_key_merges_gm_two_plant_production_cuts():
    gm_a = {
        "entity": "通用汽车",
        "date": "2026-05-27",
        "display_title_zh": "通用汽车再削减两家工厂产量",
    }
    gm_b = {
        "entity": "通用汽车",
        "date": "2026-05-27",
        "display_title_zh": "通用汽车削减两家工厂产量",
    }

    assert canonical_event_key(gm_a) == canonical_event_key(gm_b)
    assert fallback_english_title(gm_a) == ""


def test_canonical_external_id_ignores_backfilled_english_title():
    base = {
        "entity": "通用汽车",
        "date": "2026-05-27",
        "display_title_zh": "通用汽车削减两家工厂产量",
    }
    with_english = {**base, "english_title": "General Motors cuts production at two plants"}

    assert canonical_event_key(base) == canonical_event_key(with_english)
    assert canonical_external_id(base) == canonical_external_id(with_english)


def test_semantic_signature_merges_bmw_korea_engine_fire_ban_variants():
    ban = {
        "entity": "宝马",
        "date": "2026-05-23",
        "display_title_zh": "宝马发动机起火导致韩国禁令",
    }
    sales_ban = {
        "entity": "BMW",
        "date": "2026-05-23",
        "display_title_zh": "宝马发动机起火导致韩国禁售",
    }

    assert semantic_event_signature(ban)
    assert canonical_event_key(ban) == canonical_event_key(sales_ban)
    assert canonical_external_id(ban) == canonical_external_id(sales_ban)


def test_semantic_signature_merges_tesla_sweden_strike_scale_back_variants():
    variants = [
        "特斯拉在瑞典的长期罢工被工会缩减规模",
        "瑞典特斯拉罢工行动缩减",
        "IF Metall部分缩减在瑞典针对特斯拉的罢工行动",
        "特斯拉瑞典罢工规模缩减，IF Metall要求机械师复工",
    ]
    keys = {
        canonical_event_key({
            "entity": "特斯拉",
            "date": "2026-05-29",
            "display_title_zh": title,
        })
        for title in variants
    }

    assert len(keys) == 1


def test_original_english_title_only_uses_non_chinese_source_title():
    english_event = {
        "original_language": "英语",
        "original_title": "Volkswagen plans German plant closures and major job cuts",
        "core_event_title_en": "Volkswagen restructuring",
        "display_title_zh": "大众汽车拟关闭德国工厂并裁员",
    }
    chinese_event = {
        "original_language": "中文",
        "original_title": "大众汽车拟关闭德国工厂并裁员",
        "core_event_title_en": "Volkswagen restructuring",
        "display_title_zh": "大众汽车拟关闭德国工厂并裁员",
    }

    assert original_english_title(english_event) == "Volkswagen plans German plant closures and major job cuts"
    assert fallback_english_title(chinese_event) == ""


def test_clean_notion_dry_run_does_not_update_pages(monkeypatch):
    garbage_page = {
        "id": "garbage",
        "last_edited_time": "2026-06-29T00:00:00.000Z",
        "properties": {
            "标题": {"type": "title", "title": [{"plain_text": "今日无风险事件"}]},
            "Entity": {"type": "select", "select": {"name": "其他"}},
            "Date": {"type": "date", "date": {"start": "2026-06-28"}},
            "Sources": {"type": "rich_text", "rich_text": []},
            "English Title": {"type": "rich_text", "rich_text": []},
            "External ID": {"type": "rich_text", "rich_text": []},
        },
    }
    notion = MagicMock()
    monkeypatch.setattr(clean_notion, "_query_all_pages", lambda *_: [garbage_page])

    result = clean_notion.clean_database(notion, "db", apply=False)

    assert result["garbage"] == 1
    notion.pages.update.assert_not_called()


def test_clean_notion_clears_generated_english_title(monkeypatch):
    page = {
        "id": "gm-production-cut",
        "last_edited_time": "2026-05-27T00:00:00.000Z",
        "properties": {
            "标题": {"type": "title", "title": [{"plain_text": "通用汽车削减两家工厂产量"}]},
            "Entity": {"type": "select", "select": {"name": "通用汽车"}},
            "Date": {"type": "date", "date": {"start": "2026-05-27"}},
            "Sources": {"type": "rich_text", "rich_text": []},
            "English Title": {
                "type": "rich_text",
                "rich_text": [{"plain_text": "General Motors cuts production at two plants"}],
            },
            "External ID": {
                "type": "rich_text",
                "rich_text": [{"plain_text": canonical_external_id({
                    "entity": "通用汽车",
                    "date": "2026-05-27",
                    "display_title_zh": "通用汽车削减两家工厂产量",
                })}],
            },
        },
    }
    notion = MagicMock()
    monkeypatch.setattr(clean_notion, "_query_all_pages", lambda *_: [page])

    result = clean_notion.clean_database(notion, "db", apply=False)

    assert result["english_title_updates"] == 1
    notion.pages.update.assert_not_called()


def test_clean_notion_converts_markdown_sources_to_links(monkeypatch):
    page = {
        "id": "source-link-page",
        "last_edited_time": "2026-06-28T00:00:00.000Z",
        "properties": {
            "标题": {"type": "title", "title": [{"plain_text": "大众汽车拟关闭德国工厂"}]},
            "Entity": {"type": "select", "select": {"name": "大众汽车"}},
            "Date": {"type": "date", "date": {"start": "2026-06-28"}},
            "Sources": {
                "type": "rich_text",
                "rich_text": [{"plain_text": "[Reuters](https://reuters.com/vw), Bloomberg"}],
            },
            "English Title": {"type": "rich_text", "rich_text": []},
            "External ID": {
                "type": "rich_text",
                "rich_text": [{"plain_text": canonical_external_id({
                    "entity": "大众汽车",
                    "date": "2026-06-28",
                    "display_title_zh": "大众汽车拟关闭德国工厂",
                })}],
            },
        },
    }
    notion = MagicMock()
    monkeypatch.setattr(clean_notion, "_query_all_pages", lambda *_: [page])

    result = clean_notion.clean_database(notion, "db", apply=False)

    assert result["source_updates"] == 1
    notion.pages.update.assert_not_called()


def test_attach_source_urls_fills_missing_event_links():
    event = {
        "sources": [
            {"name": "Reuters"},
            {"name": "Bloomberg"},
        ],
        "source_urls": [
            "https://reuters.com/article",
            "https://bloomberg.com/story",
        ],
    }

    import sys
    import types

    radar_infra = types.ModuleType("radar_infra")
    radar_infra_llm = types.ModuleType("radar_infra.llm")
    radar_infra_llm.create_provider = lambda: None
    radar_infra_llm.create_cheap_provider = lambda: None
    radar_infra_llm.create_llm_retry_decorator = lambda max_attempts=1: (lambda fn: fn)
    radar_infra_llm.BaseLLMProvider = object
    radar_infra_llm.TokenUsage = object
    sys.modules.setdefault("radar_infra", radar_infra)
    sys.modules.setdefault("radar_infra.llm", radar_infra_llm)

    from esg_intelligence_agent import ESGIntelligenceAgent

    ESGIntelligenceAgent._attach_source_urls(event, [])
    assert event["sources"][0]["url"] == "https://reuters.com/article"
    assert event["sources"][1]["url"] == "https://bloomberg.com/story"


def test_resolve_news_url_with_googlenewsdecoder():
    # Test resolving a real/mocked Google News URL with the new encoder using googlenewsdecoder
    google_url = "https://news.google.com/rss/articles/CBMilgFBVV95cUxOTHpMdGRuQUlyUEctYml3aE5ST0wxMUxlMkIwSmlvTnhSNkRPb2dzNHFpNllFMk5ZbVlpTi0yUU9RZFhEZWs5dTNTY3BLaTBHSG56S3NfbUZZdVRiNXFFQW56NjVjMXJNSWVHSF9UWHkwVm1iUzh4ck1hTEtubTJKbUZ2cWVWZjVxSFlVZXNaTkl4TF9iOFE?oc=5"
    expected = "https://www.business-humanrights.org/en/from-us/briefings/transition-minerals-tracker-2026/"
    assert resolve_news_url(google_url) == expected


def test_resolve_news_url_filters_googleapis_and_css():
    # Verify that fonts.googleapis.com URL or similar stylesheets are NOT returned as valid news URLs
    invalid_url = "https://fonts.googleapis.com/css?family=Google+Sans+Text"
    from esg_agent.fetchers import _is_valid_news_url
    assert not _is_valid_news_url(invalid_url)

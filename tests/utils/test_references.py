"""Unit tests for backend/utils/references.py — pure functions, no external dependencies."""

import pytest
from backend.utils.references import (
    clean_title,
    normalize_url,
    extract_domain_name,
    extract_title_from_url_path,
    extract_website_name_from_domain,
    format_reference_for_markdown,
    extract_link_info,
)


# ── clean_title ──────────────────────────────────────────────────────

class TestCleanTitle:
    def test_strips_trailing_periods_and_quotes(self):
        assert clean_title('"Breaking News."') == "Breaking News"
        assert clean_title("Company Report.'") == "Company Report"

    def test_strips_date_prefix(self):
        assert clean_title("2024-01-15 Tesla Announces") == "Tesla Announces"
        assert clean_title("2024 01 15 BMW Update") == "BMW Update"

    def test_preserves_valid_title(self):
        assert clean_title("Huayou Cobalt Reports Q4 Earnings") == "Huayou Cobalt Reports Q4 Earnings"

    def test_empty_input_returns_empty(self):
        assert clean_title("") == ""

    def test_cleans_dash_prefix_after_date_removal(self):
        assert clean_title("2024-06-18 - Some Event") == "Some Event"


# ── normalize_url ────────────────────────────────────────────────────

class TestNormalizeUrl:
    def test_strips_query_and_fragment(self):
        result = normalize_url("https://example.com/page?a=1&b=2#section")
        assert result == "https://example.com/page"

    def test_adds_https_when_missing(self):
        result = normalize_url("example.com/path")
        assert result == "https://example.com/path"

    def test_preserves_already_normalized_url(self):
        result = normalize_url("https://www.testcorp.com/about")
        assert result == "https://www.testcorp.com/about"

    def test_empty_url_returns_empty(self):
        assert normalize_url("") == ""

    def test_strips_trailing_slash(self):
        result = normalize_url("https://example.com/page/")
        assert result == "https://example.com/page"


# ── extract_domain_name ──────────────────────────────────────────────

class TestExtractDomainName:
    def test_extracts_from_standard_url(self):
        assert extract_domain_name("https://www.tavily.com/search") == "Tavily"

    def test_extracts_from_url_without_www(self):
        assert extract_domain_name("https://blog.example.org/post") == "Blog"

    def test_extracts_from_http_url(self):
        assert extract_domain_name("http://news.google.com/rss") == "News"

    def test_fallback_on_error(self):
        result = extract_domain_name("not-a-valid-url-!@#")
        assert isinstance(result, str)
        assert len(result) > 0


# ── extract_title_from_url_path ──────────────────────────────────────

class TestExtractTitleFromUrlPath:
    def test_extracts_from_hyphenated_path(self):
        result = extract_title_from_url_path("https://site.com/tesla-announces-new-battery")
        assert "Tesla" in result

    def test_extracts_from_underscored_path(self):
        result = extract_title_from_url_path("https://site.com/huayou_cobalt_update")
        assert "Huayou" in result

    def test_empty_path_returns_empty(self):
        assert extract_title_from_url_path("https://site.com") == ""

    def test_truncates_long_title(self):
        long_path = "https://site.com/" + "a" * 150
        result = extract_title_from_url_path(long_path)
        assert len(result) <= 103  # 100 + "..." = 103


# ── extract_website_name_from_domain ─────────────────────────────────

class TestExtractWebsiteName:
    def test_strips_www_prefix(self):
        assert extract_website_name_from_domain("www.example.com") == "Example"

    def test_extracts_without_www(self):
        assert extract_website_name_from_domain("blog.company.org") == "Blog"


# ── format_reference_for_markdown ────────────────────────────────────

class TestFormatReferenceForMarkdown:
    def test_formats_complete_entry(self):
        entry = {
            "website": "Reuters",
            "title": "Tesla Stock Rises",
            "url": "https://reuters.com/tesla"
        }
        result = format_reference_for_markdown(entry)
        assert result == '* Reuters. "Tesla Stock Rises." https://reuters.com/tesla'

    def test_falls_back_to_url_when_title_empty(self):
        entry = {
            "website": "Bloomberg",
            "title": "",
            "url": "https://bloomberg.com/news/item"
        }
        result = format_reference_for_markdown(entry)
        assert "Bloomberg" in result
        assert "https://bloomberg.com/news/item" in result

    def test_extracts_domain_when_website_missing(self):
        entry = {
            "website": "",
            "title": "Some Title",
            "url": "https://unknown-site.com/article"
        }
        result = format_reference_for_markdown(entry)
        assert "Unknown" in result  # extracted from domain
        assert "Some Title" in result


# ── extract_link_info ────────────────────────────────────────────────

class TestExtractLinkInfo:
    def test_extracts_standard_markdown_link(self):
        title, url = extract_link_info("[Click here](https://example.com)")
        assert title == "Click here"
        assert url == "https://example.com"

    def test_extracts_mla_style_reference(self):
        line = '* Reuters. "Breaking News." [Link](https://reuters.com)'
        title, url = extract_link_info(line)
        assert "Reuters" in title
        assert url == "https://reuters.com"

    def test_no_link_returns_empty(self):
        title, url = extract_link_info("Just plain text without any link")
        assert title == ""
        assert url == ""


# ── Reference processing from state ──────────────────────────────────

class TestProcessReferencesFromSearchResults:
    def test_returns_empty_for_empty_state(self):
        from backend.utils.references import process_references_from_search_results
        urls, titles, info = process_references_from_search_results({})
        assert urls == []
        assert titles == {}
        assert info == {}

    def test_deduplicates_by_normalized_url(self):
        from backend.utils.references import process_references_from_search_results
        state = {
            "curated_company_data": {
                "https://a.com/page?ref=123": {
                    "evaluation": {"overall_score": 0.9},
                    "title": "Article A",
                    "url": "https://a.com/page?ref=123",
                },
                "https://a.com/page?ref=456": {
                    "evaluation": {"overall_score": 0.7},
                    "title": "Article A Duplicate",
                    "url": "https://a.com/page?ref=456",
                },
            }
        }
        urls, titles, info = process_references_from_search_results(state)
        # Both collapse to "https://a.com/page"; only highest-score is kept
        assert len(urls) == 1
        assert urls[0] == "https://a.com/page"

    def test_sorts_by_score_descending(self):
        from backend.utils.references import process_references_from_search_results
        state = {
            "curated_news_data": {
                "https://low.com": {
                    "evaluation": {"overall_score": 0.3},
                    "title": "Low Score",
                    "url": "https://low.com",
                },
                "https://high.com": {
                    "evaluation": {"overall_score": 0.95},
                    "title": "High Score",
                    "url": "https://high.com",
                },
                "https://mid.com": {
                    "evaluation": {"overall_score": 0.5},
                    "title": "Mid Score",
                    "url": "https://mid.com",
                },
            }
        }
        urls, titles, info = process_references_from_search_results(state)
        assert urls[0] == "https://high.com"
        assert urls[-1] == "https://low.com"

    def test_limits_to_10_references(self):
        from backend.utils.references import process_references_from_search_results
        state = {
            "curated_company_data": {
                f"https://site{i}.com": {
                    "evaluation": {"overall_score": 0.9 - i * 0.01},
                    "title": f"Article {i}",
                    "url": f"https://site{i}.com",
                }
                for i in range(15)
            }
        }
        urls, titles, info = process_references_from_search_results(state)
        assert len(urls) == 10

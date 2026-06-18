"""Shared fixtures for all tests."""

import pytest
from typing import Dict, Any


@pytest.fixture
def sample_research_state() -> Dict[str, Any]:
    """Minimal ResearchState-like dict for testing curator and references."""
    return {
        "company": "TestCorp",
        "company_url": "https://testcorp.com",
        "hq_location": "Shanghai",
        "industry": "Battery Materials",
        "curated_company_data": {},
        "curated_industry_data": {},
        "curated_financial_data": {},
        "curated_news_data": {},
    }


@pytest.fixture
def sample_urls() -> list:
    """A diverse set of URLs for testing normalization and extraction."""
    return [
        "https://www.example.com/path/to/article?utm_source=twitter&ref=home",
        "http://blog.test.com/2024/01/15-news-item/",
        "https://api.service.io/v2/data?key=value#section",
        "www.missing-scheme.com/page",
        "",  # empty
        "not-a-url",
    ]

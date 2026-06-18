#!/usr/bin/env python3
"""
SourcingEngine — 声明式配置驱动的 ESG 多轨道供料引擎。
支持 Google News RSS 动态搜索 和 静态 HTML 靶向抓取 两种轨道。
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class SourcingEngine:
    """加载 esg_sources.yaml，按轨道类型分发抓取，汇总标准化结果。"""

    def __init__(self, config_path: str = None) -> None:
        if config_path is None:
            config_path = str(Path(__file__).parent / "esg_sources.yaml")
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)

        self.version: str = raw.get("version", "unknown")
        self.sources: List[Dict[str, Any]] = [
            src for src in raw.get("esg_sources", []) if src.get("enabled", False)
        ]
        logger.info(
            "SourcingEngine v%s loaded — %d active source(s)",
            self.version,
            len(self.sources),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_all_active_sources(self) -> List[Dict[str, Any]]:
        """遍历所有启用的源，汇总标准化结果列表。"""
        all_results: List[Dict[str, Any]] = []

        for src in self.sources:
            src_type = src.get("type", "")
            src_id = src.get("id", "unknown")

            try:
                if src_type == "google_news_rss":
                    items = self._fetch_google_news_rss(src)
                elif src_type == "html":
                    items = self._fetch_html_target(src)
                else:
                    logger.warning("Unknown source type '%s' for %s — skipped", src_type, src_id)
                    continue

                logger.info("[%s] collected %d item(s)", src_id, len(items))
                all_results.extend(items)

            except Exception:
                logger.exception("[%s] fetch failed — continuing to next source", src_id)

        logger.info("Total aggregated items: %d", len(all_results))
        return all_results

    # ------------------------------------------------------------------
    # Track 1: Google News RSS
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_time_window(time_window: str) -> timedelta:
        """将 '7d' / '24h' 之类的简写转为 timedelta。"""
        match = re.match(r"^(\d+)\s*(d|h|w)$", time_window.strip().lower())
        if not match:
            logger.warning("Unrecognized time_window '%s', falling back to 7 days", time_window)
            return timedelta(days=7)
        value = int(match.group(1))
        unit = match.group(2)
        if unit == "h":
            return timedelta(hours=value)
        elif unit == "w":
            return timedelta(weeks=value)
        return timedelta(days=value)

    def _fetch_google_news_rss(self, source: Dict[str, Any]) -> List[Dict[str, Any]]:
        from urllib.parse import quote

        query_raw: str = source.get("query", "")
        time_window_str: str = source.get("time_window", "7d")
        source_id: str = source.get("id", "unknown")

        encoded_query = quote(query_raw, safe="")
        rss_url = (
            f"https://news.google.com/rss/search?"
            f"q={encoded_query}&hl=en-US&gl=US&ceid=US:en"
        )
        logger.debug("[%s] RSS URL: %s", source_id, rss_url)

        feed = feedparser.parse(rss_url)
        cutoff = datetime.now(timezone.utc) - self._parse_time_window(time_window_str)

        results: List[Dict[str, Any]] = []
        for entry in feed.entries:
            # Parse published date — feedparser struct_time stored as plain tuple
            pub_date: Optional[datetime] = None
            parsed: Optional[Any] = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                parsed = entry.published_parsed
            elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                parsed = entry.updated_parsed

            if parsed:
                try:
                    pub_date = datetime(
                        int(parsed[0]), int(parsed[1]), int(parsed[2]),
                        int(parsed[3]), int(parsed[4]), int(parsed[5]),
                        tzinfo=timezone.utc,
                    )
                except (IndexError, TypeError, ValueError):
                    pass

            if pub_date and pub_date < cutoff:
                continue

            title = getattr(entry, "title", "")
            link = getattr(entry, "link", "")
            summary = getattr(entry, "summary", "")
            content_body = f"{title}\n{summary}".strip()

            results.append({
                "title": title,
                "link": link,
                "pub_date": pub_date.isoformat() if pub_date else datetime.now(timezone.utc).isoformat(),
                "source_id": source_id,
                "content": content_body,
            })

        return results

    # ------------------------------------------------------------------
    # Track 2: Static HTML Target
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_url(raw_url: str) -> str:
        """从可能包含 Markdown 链接格式的字符串中提取纯净 URL。"""
        # 匹配 Markdown 链接 [text](url) 中的 url
        md_match = re.search(r"\[.*?\]\((https?://[^\s)]+)\)", raw_url)
        if md_match:
            return md_match.group(1)
        return raw_url.strip()

    def _fetch_html_target(self, source: Dict[str, Any]) -> List[Dict[str, Any]]:
        source_id: str = source.get("id", "unknown")
        raw_url: str = source.get("url", "")
        dom_selector: str = source.get("dom_selector", "body")

        clean_url = self._extract_url(raw_url)
        logger.debug("[%s] fetching %s → selector '%s'", source_id, clean_url, dom_selector)

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        resp = requests.get(clean_url, headers=headers, timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        # 极致降噪：剪除所有噪音标签
        for noise_tag in soup.find_all(["script", "style", "nav", "footer", "header"]):
            noise_tag.decompose()

        # 定位核心内容区域
        target_area = soup.select_one(dom_selector)
        if target_area is None:
            logger.warning(
                "[%s] dom_selector '%s' matched nothing — falling back to body text",
                source_id,
                dom_selector,
            )
            target_area = soup.find("body") or soup

        clean_text = target_area.get_text(strip=True)

        return [{
            "title": "HTML Target Spec",
            "link": clean_url,
            "pub_date": datetime.now(timezone.utc).isoformat(),
            "source_id": source_id,
            "content": clean_text,
        }]


# ==========================================================================
# 本地沙盒测试
# ==========================================================================
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    engine = SourcingEngine()
    all_items = engine.fetch_all_active_sources()

    print(f"\n{'='*60}")
    print(f"Total items fetched: {len(all_items)}")
    print(f"{'='*60}\n")

    for i, item in enumerate(all_items[:2], start=1):
        print(f"--- Item #{i} ---")
        print(f"Title: {item.get('title', 'N/A')}")
        print(f"Link: {item.get('link', 'N/A')}")
        print(f"Source: {item.get('source_id', 'N/A')}")
        print(f"Date: {item.get('pub_date', 'N/A')}")
        content_preview = (item.get('content', '') or '')[:500]
        print(f"Content (first 500 chars): {content_preview}")
        print()
#!/usr/bin/env python3
"""
SourcingEngine — 声明式配置驱动的 ESG 多轨道供料引擎。
支持 Google News RSS 动态搜索 和 静态 HTML 靶向抓取 两种轨道。
"""

import base64
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from esg_agent.fetchers import resolve_news_url

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

        # 加载轻量级 URL 记忆库 (Persistent Cache)
        self.cache_path = Path(__file__).parent / "logs" / "processed_urls.json"
        self.processed_urls: Dict[str, str] = {}
        if self.cache_path.exists():
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    self.processed_urls = json.load(f)
            except Exception as e:
                logger.warning("Failed to load processed_urls.json: %s", e)

        # 整理/剪除 14 天前的记录
        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        pruned_cache = {}
        for u, ts_str in self.processed_urls.items():
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts >= cutoff:
                    pruned_cache[u] = ts_str
            except Exception:
                pruned_cache[u] = ts_str
        self.processed_urls = pruned_cache

    def _decode_google_news_url(self, url: str) -> str:
        if "news.google.com/rss/articles/" not in url:
            return url
        match = re.search(r'articles/([^?]+)', url)
        if match:
            encoded_str = match.group(1)
            padding = 4 - (len(encoded_str) % 4)
            if padding != 4:
                encoded_str += "=" * padding
            try:
                decoded_bytes = base64.urlsafe_b64decode(encoded_str)
                # Google 的 payload 混合了不可见字符，使用正则直接提取其中的 http 链接
                decoded_str = decoded_bytes.decode('latin1')
                url_match = re.search(r'(https?://[^\s\x00-\x1f\x7f-\xff]+)', decoded_str)
                if url_match:
                    return url_match.group(1)
            except Exception:
                pass
        return url

    def _save_processed_urls(self, urls: List[str]) -> None:
        """保存新抓取到的 URL，并更新/清洗 14 天前的记录"""
        now_str = datetime.now(timezone.utc).isoformat()
        for url in urls:
            if url:
                self.processed_urls[url] = now_str

        # 整理/剪除 14 天前的记录
        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        pruned_cache = {}
        for u, ts_str in self.processed_urls.items():
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts >= cutoff:
                    pruned_cache[u] = ts_str
            except Exception:
                pruned_cache[u] = ts_str
        self.processed_urls = pruned_cache

        # 确保 logs 目录存在并写入
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self.processed_urls, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("Failed to save processed_urls.json: %s", e)

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

        # 汇总数据后，将抓取到的新 URL 写入持久化记忆库
        self._save_processed_urls([item["link"] for item in all_results])

        logger.info("Total aggregated items: %d", len(all_results))
        return all_results

    def fetch_from_prebuilt_urls(
        self, url_entries: List[Dict[str, Any]], time_window: str = "24h"
    ) -> List[Dict[str, Any]]:
        """从预构建的 RSS URL 列表并发抓取，用于 query_tasks + AI 动态发现等非静态轨道。

        使用 ThreadPoolExecutor 并发抓取所有 RSS URL，将 300+ 个串行请求
        从 ~25 分钟压缩到 ~2-3 分钟。

        Args:
            url_entries: 每条包含 {"url": str, "source_id": str}
            time_window: 全局时间窗，应用于所有 URL（默认 24h）

        Returns:
            标准化 items 列表，与 fetch_all_active_sources() 格式一致。
        """
        if not url_entries:
            return []

        from concurrent.futures import ThreadPoolExecutor, as_completed

        cutoff = datetime.now(timezone.utc) - self._parse_time_window(time_window)

        def _fetch_one(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
            """单线程任务：抓取一个 RSS URL 并过滤时间窗内的条目。"""
            rss_url = str(entry.get("url", "")).strip()
            source_id = str(entry.get("source_id", "dynamic_query"))

            if not rss_url:
                return []

            items: List[Dict[str, Any]] = []
            try:
                feed = feedparser.parse(rss_url)
                for feed_entry in feed.entries:
                    pub_date: Optional[datetime] = None
                    parsed: Optional[Any] = None
                    if hasattr(feed_entry, "published_parsed") and feed_entry.published_parsed:
                        parsed = feed_entry.published_parsed
                    elif hasattr(feed_entry, "updated_parsed") and feed_entry.updated_parsed:
                        parsed = feed_entry.updated_parsed

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

                    title = getattr(feed_entry, "title", "")
                    raw_link = getattr(feed_entry, "link", "")
                    decoded_link = self._decode_google_news_url(raw_link)
                    link = resolve_news_url(decoded_link)

                    # 核心拦截逻辑：检查已存在于 processed_urls.json
                    if link in self.processed_urls:
                        continue

                    summary = getattr(feed_entry, "summary", "")
                    content_body = f"{title}\n{summary}".strip()

                    items.append({
                        "title": title,
                        "link": link,
                        "pub_date": pub_date.isoformat() if pub_date else datetime.now(timezone.utc).isoformat(),
                        "source_id": source_id,
                        "content": content_body,
                    })

                if items:
                    logger.debug("[%s] collected %d item(s)", source_id, len(items))

            except Exception:
                logger.debug("[%s] fetch skipped (non-critical)", source_id)

            return items

        # 并发抓取：max_workers=20 在 Google News RSS 的网络 I/O 场景下效果最佳
        # 过高的 worker 数量可能触发 Google 的速率限制
        MAX_WORKERS = 20
        all_results: List[Dict[str, Any]] = []
        total_urls = len(url_entries)

        logger.info(
            "Dynamic URL fetch (concurrent): %d URLs with %d workers",
            total_urls, MAX_WORKERS,
        )

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_entry = {
                executor.submit(_fetch_one, entry): entry
                for entry in url_entries
                if str(entry.get("url", "")).strip()
            }
            for future in as_completed(future_to_entry):
                try:
                    items = future.result()
                    all_results.extend(items)
                except Exception as exc:
                    entry = future_to_entry[future]
                    logger.debug(
                        "[%s] future raised %s",
                        entry.get("source_id", "?"), exc,
                    )

        # 汇总数据后，将抓取到的新 URL 写入持久化记忆库
        self._save_processed_urls([item["link"] for item in all_results])

        logger.info(
            "Dynamic URL fetch complete: %d/%d URLs processed, %d total items",
            total_urls, total_urls, len(all_results),
        )
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
            raw_link = getattr(entry, "link", "")
            decoded_link = self._decode_google_news_url(raw_link)
            link = resolve_news_url(decoded_link)

            # 核心拦截逻辑：检查已存在于 processed_urls.json
            if link in self.processed_urls:
                continue

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

        # 核心拦截逻辑：检查已存在于 processed_urls.json
        if clean_url in self.processed_urls:
            logger.info("[%s] HTML URL '%s' already in processed cache — skipped", source_id, clean_url)
            return []

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

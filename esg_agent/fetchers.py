"""
esg_agent.fetchers — 新闻抓取与内容提取器
"""

from __future__ import annotations
import html as _html
import logging
import re
from typing import Optional
from urllib.parse import unquote, parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from .config import FETCH_HEADERS

logger = logging.getLogger("esg_agent")


_HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.I)
_GOOGLE_HOST_RE = re.compile(r"(^|\.)(google|gstatic|googleapis|googleusercontent|youtube|ytimg|google-analytics|googletagmanager)\.", re.I)


def _is_google_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return bool(_GOOGLE_HOST_RE.search(host))


def _is_valid_news_url(url: str) -> bool:
    if not url:
        return False
    
    # 1. 基础谷歌域名过滤
    if _is_google_url(url):
        return False
        
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
    except Exception:
        return False
        
    # 2. 拦截第三方流量统计、社交分享、广告追踪等非新闻域名
    ignored_hosts = (
        "google-analytics.com",
        "googletagmanager.com",
        "googlesyndication.com",
        "googleadservices.com",
        "doubleclick.net",
        "facebook.com",
        "twitter.com",
        "linkedin.com",
    )
    if any(ih in host for ih in ignored_hosts):
        return False
        
    # 3. 拦截静态资源后缀，避免抓取到 js/css/图片等资产
    static_extensions = (
        ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", 
        ".svg", ".ico", ".woff", ".woff2", ".mp4", ".mp3"
    )
    if path.endswith(static_extensions):
        return False
        
    return True


def _clean_candidate_url(raw: str) -> str:
    cleaned = _html.unescape(unquote(str(raw or "").strip()))
    cleaned = cleaned.rstrip(").,;]}\"'")
    return cleaned


def _extract_original_from_html(html_text: str) -> str:
    """Best-effort extraction of the publisher URL from a Google News page."""
    if not html_text:
        return ""

    soup = BeautifulSoup(html_text, "html.parser")
    for selector in (
        ("link", {"rel": "canonical"}),
        ("meta", {"property": "og:url"}),
        ("meta", {"name": "twitter:url"}),
    ):
        tag = soup.find(*selector)
        value = ""
        if tag:
            value = tag.get("href") or tag.get("content") or ""
        value = _clean_candidate_url(value)
        if value and value.startswith("http") and _is_valid_news_url(value):
            return value

    for a in soup.find_all("a", href=True):
        href = _clean_candidate_url(a.get("href", ""))
        if href.startswith("./articles/"):
            continue
        if href.startswith("http") and _is_valid_news_url(href):
            return href

    for candidate in _HTTP_URL_RE.findall(html_text):
        candidate = _clean_candidate_url(candidate)
        if candidate.startswith("http") and _is_valid_news_url(candidate):
            return candidate
    return ""


def resolve_news_url(url: str, timeout: int = 5) -> str:
    if not url or "news.google.com" not in url:
        return url

    # 1. 优先尝试使用 googlenewsdecoder 包进行解码
    try:
        from googlenewsdecoder import GoogleDecoder
        decoder = GoogleDecoder()
        res = decoder.decode_google_news_url(url)
        if res.get("status") and res.get("decoded_url"):
            decoded_url = res["decoded_url"]
            if decoded_url.startswith("http") and _is_valid_news_url(decoded_url):
                return decoded_url
    except Exception as e:
        logger.debug(f"googlenewsdecoder failed: {e}")

    if "news.google.com/rss/articles/" in url:
        import base64
        match = re.search(r'articles/([^?]+)', url)
        if match:
            encoded_str = match.group(1)
            padding = 4 - (len(encoded_str) % 4)
            if padding != 4:
                encoded_str += "=" * padding
            try:
                decoded_bytes = base64.urlsafe_b64decode(encoded_str)
                decoded_str = decoded_bytes.decode('latin1')
                url_match = re.search(r'(https?://[^\s\x00-\x1f\x7f-\xff]+)', decoded_str)
                if url_match:
                    return url_match.group(1)
            except Exception:
                pass


    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ("url", "u", "q"):
        for val in qs.get(key, []):
            candidate = _clean_candidate_url(val)
            if candidate.startswith("http") and _is_valid_news_url(candidate):
                return candidate

    try:
        resp = requests.head(url, headers=FETCH_HEADERS, allow_redirects=True, timeout=timeout)
        final = resp.url
        if final != url and _is_valid_news_url(final) and len(final) > 15 and final.lower().startswith("http"):
            return final
    except Exception as exc:
        logger.debug(f"URL HEAD resolve failed: {exc}")

    try:
        resp = requests.get(url, headers=FETCH_HEADERS, allow_redirects=True, timeout=timeout)
        final = resp.url
        if final != url and _is_valid_news_url(final) and len(final) > 15 and final.lower().startswith("http"):
            return final
        candidate = _extract_original_from_html(resp.text)
        if candidate:
            return candidate
    except Exception as exc:
        logger.debug(f"URL GET resolve failed: {exc}")
    return url


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    unescaped = _html.unescape(raw)
    try:
        text = BeautifulSoup(unescaped, "html.parser").get_text(separator=" ", strip=True)
    except Exception:
        text = re.sub(r"<[^>]+>", "", unescaped)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()




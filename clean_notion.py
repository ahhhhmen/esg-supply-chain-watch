#!/usr/bin/env python3
"""
clean_notion.py — audit and clean the ESG Notion database.

Default mode is dry-run. Use --apply to archive duplicate/spam pages and update
Google News source links to publisher URLs.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

from notion_client import Client as NotionClient

from backend.utils.references import clean_title
from esg_agent.canonical import canonical_event_key, canonical_external_id, fallback_english_title
from esg_agent.fetchers import resolve_news_url
from esg_agent.filters import EntityFilter
from notion_mapping import EXTERNAL_ID


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("clean_notion")


TITLE_PROP = "标题"
ENTITY_PROP = "Entity"
DATE_PROP = "Date"
SOURCES_PROP = "Sources"
MATERIALITY_PROP = "Materiality"
ENGLISH_TITLE_PROP = "English Title"

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_SOURCE_SUFFIX_RE = re.compile(r"\s*\((?:媒体|新闻源|官方|聚合)(?:/[^\)]+)?\)\s*$")
_NOISE_TITLE_RE = re.compile(
    r"今日无风险事件|无新增风险|系统巡检完成|监控矩阵盲区|token 消耗",
    re.IGNORECASE,
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_GENERATED_ENGLISH_TITLES = {
    "Volkswagen plans German plant closures and major job cuts",
    "UAW strike at General Motors truck supplier plant",
    "General Motors cuts production at two plants",
    "Mercedes-Benz faces potential U.S. sales ban over China ownership",
    "Mercedes-Benz GLS 450 explosion incident",
    "Tesla German plant faces underutilized production capacity",
    "CATL Hungary Debrecen battery plant faces regulatory scrutiny",
    "CMOC Congo mining labor dispute exposes supply risk",
    "BMW engine fire incident leads to South Korea sales ban",
    "Tesla Sweden strike action scaled back",
    "Tesla",
    "BMW",
    "CATL",
}
@dataclass
class PageRecord:
    page_id: str
    title: str
    entity: str
    date: str
    sources: str
    english_title: str
    external_id: str
    materiality: str
    last_edited_time: str
    raw: dict


def _plain_text(value: dict) -> str:
    if not isinstance(value, dict):
        return ""
    typ = value.get("type")
    if typ == "title":
        return "".join(t.get("plain_text", "") for t in value.get("title", []))
    if typ == "rich_text":
        return "".join(t.get("plain_text", "") for t in value.get("rich_text", []))
    if typ == "select":
        sel = value.get("select") or {}
        return sel.get("name", "")
    if typ == "date":
        date = value.get("date") or {}
        return str(date.get("start", "") or "")
    if typ == "checkbox":
        return str(bool(value.get("checkbox")))
    return ""


def _page_to_record(page: dict) -> PageRecord:
    props = page.get("properties", {})
    return PageRecord(
        page_id=page.get("id", ""),
        title=_plain_text(props.get(TITLE_PROP, {})).strip(),
        entity=_plain_text(props.get(ENTITY_PROP, {})).strip(),
        date=_plain_text(props.get(DATE_PROP, {})).strip()[:10],
        sources=_plain_text(props.get(SOURCES_PROP, {})).strip(),
        english_title=_plain_text(props.get(ENGLISH_TITLE_PROP, {})).strip(),
        external_id=_plain_text(props.get(EXTERNAL_ID, {})).strip(),
        materiality=_plain_text(props.get(MATERIALITY_PROP, {})).strip(),
        last_edited_time=page.get("last_edited_time", ""),
        raw=page,
    )


def _query_all_pages(notion: Any, database_id: str) -> list[dict]:
    """Query every non-archived row, supporting both database and data_source APIs."""
    query_fn = None
    query_kwargs: dict[str, Any] = {}
    try:
        db_info = notion.databases.retrieve(database_id=database_id)
        data_sources = db_info.get("data_sources", [])
        if data_sources:
            query_fn = notion.data_sources.query
            query_kwargs = {"data_source_id": data_sources[0]["id"]}
    except Exception:
        pass

    if query_fn is None:
        query_fn = notion.databases.query
        query_kwargs = {"database_id": database_id}

    pages: list[dict] = []
    cursor: Optional[str] = None
    while True:
        kwargs = dict(query_kwargs)
        if cursor:
            kwargs["start_cursor"] = cursor
        kwargs["page_size"] = 100
        result = query_fn(**kwargs)
        pages.extend(result.get("results", []))
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")
    return pages


def _dedupe_key(record: PageRecord) -> str:
    event = {
        "entity": record.entity,
        "date": record.date,
        "display_title_zh": clean_title(record.title) or record.title,
    }
    return canonical_event_key(event)


def _is_garbage(record: PageRecord) -> bool:
    text = f"{record.title} {record.entity} {record.sources}"
    if not record.title or not record.entity or record.entity == "其他":
        return True
    if _NOISE_TITLE_RE.search(record.title):
        return True
    if EntityFilter.is_spam(record.title, "", record.sources):
        return True
    return False


def _choose_canonical(records: list[PageRecord]) -> PageRecord:
    def score(record: PageRecord) -> tuple[int, str]:
        external = 1 if record.external_id else 0
        source_score = min(record.sources.count("http"), 5)
        title_score = min(len(record.title), 120)
        return (external * 1000 + source_score * 20 + title_score, record.last_edited_time)

    return sorted(records, key=score, reverse=True)[0]


def _resolved_sources_text(sources: str) -> tuple[str, int]:
    if not sources or "news.google.com" not in sources:
        return sources, 0

    changed = 0

    def replace(match: re.Match) -> str:
        nonlocal changed
        label = match.group(1)
        url = match.group(2)
        resolved = resolve_news_url(url)
        if resolved != url:
            changed += 1
        return f"[{label}]({resolved})"

    updated = _MD_LINK_RE.sub(replace, sources)
    return updated, changed


def _normalize_source_label(label: str) -> str:
    text = str(label or "").strip()
    if not text:
        return ""
    return _SOURCE_SUFFIX_RE.sub("", text).strip()


def _normalize_sources_text(sources: str) -> str:
    if not sources:
        return sources

    def replace(match: re.Match) -> str:
        label = _normalize_source_label(match.group(1))
        url = match.group(2)
        return f"[{label}]({url})"

    return _MD_LINK_RE.sub(replace, sources)


def _sources_rich_text_from_markdown(sources: str) -> list[dict]:
    rich_text: list[dict] = []
    if not sources:
        return rich_text

    pos = 0
    for match in _MD_LINK_RE.finditer(sources):
        prefix = sources[pos:match.start()]
        if prefix.strip():
            rich_text.append({"text": {"content": prefix}})
        label = _normalize_source_label(match.group(1))
        url = match.group(2)
        if label and url:
            rich_text.append({"text": {"content": label, "link": {"url": url}}})
        elif label:
            rich_text.append({"text": {"content": label}})
        pos = match.end()

    suffix = sources[pos:]
    if suffix.strip():
        rich_text.append({"text": {"content": suffix}})
    return rich_text


def _external_id_property(record: PageRecord) -> dict:
    external_id = canonical_external_id({
        "entity": record.entity,
        "date": record.date,
        "display_title_zh": clean_title(record.title) or record.title,
    })
    return {EXTERNAL_ID: {"rich_text": [{"text": {"content": external_id}}]}}


def _record_event(record: PageRecord) -> dict:
    return {
        "entity": record.entity,
        "date": record.date,
        "display_title_zh": clean_title(record.title) or record.title,
        "english_title": record.english_title,
    }


def _expected_english_title(record: PageRecord) -> str:
    event = _record_event(record)
    event["english_title"] = ""
    return fallback_english_title(event)


def _english_title_property(value: str) -> dict:
    if not value:
        return {ENGLISH_TITLE_PROP: {"rich_text": []}}
    return {ENGLISH_TITLE_PROP: {"rich_text": [{"text": {"content": value[:2000]}}]}}


def _is_generated_english_title(record: PageRecord) -> bool:
    english = record.english_title.strip()
    if not english:
        return False
    if _CJK_RE.search(english):
        return True
    if english in _GENERATED_ENGLISH_TITLES:
        return True
    prefix = f"{record.entity} - "
    return bool(record.entity and english.startswith(prefix))


def clean_database(notion: Any, database_id: str, apply: bool = False) -> dict:
    pages = [_page_to_record(p) for p in _query_all_pages(notion, database_id)]
    logger.info("Loaded %d Notion page(s)", len(pages))

    garbage = [p for p in pages if _is_garbage(p)]
    non_garbage = [p for p in pages if p.page_id not in {g.page_id for g in garbage}]

    groups: dict[str, list[PageRecord]] = {}
    for page in non_garbage:
        groups.setdefault(_dedupe_key(page), []).append(page)

    duplicate_archives: list[PageRecord] = []
    for group in groups.values():
        if len(group) <= 1:
            continue
        canonical = _choose_canonical(group)
        duplicate_archives.extend(p for p in group if p.page_id != canonical.page_id)

    archive_ids = {p.page_id for p in garbage + duplicate_archives}
    active_keep_pages = [p for p in non_garbage if p.page_id not in archive_ids]

    source_updates: list[tuple[PageRecord, str, int]] = []
    external_id_updates: list[PageRecord] = []
    english_title_updates: list[tuple[PageRecord, str]] = []
    for page in active_keep_pages:
        updated_sources, changed = _resolved_sources_text(page.sources)
        normalized_sources = _normalize_sources_text(updated_sources)
        needs_link_rewrite = bool(_MD_LINK_RE.search(page.sources))
        if normalized_sources != page.sources or needs_link_rewrite:
            source_updates.append((page, normalized_sources, max(changed, 1)))
        expected_external_id = canonical_external_id(_record_event(page))
        if page.external_id != expected_external_id:
            external_id_updates.append(page)
        if page.english_title:
            english_title_updates.append((page, ""))

    logger.info("Garbage pages to archive: %d", len(garbage))
    logger.info("Duplicate pages to archive: %d", len(duplicate_archives))
    logger.info("Pages with Google News source links to rewrite: %d", len(source_updates))
    logger.info("Pages with missing/stale External ID to migrate: %d", len(external_id_updates))
    logger.info("Pages with blank English Title to backfill: %d", len(english_title_updates))

    for page in garbage[:20]:
        logger.info("[garbage] %s | %s | %s", page.page_id, page.entity, page.title[:80])
    for page in duplicate_archives[:20]:
        logger.info("[duplicate] %s | %s | %s | %s", page.page_id, page.entity, page.date, page.title[:80])
    for page, _, changed in source_updates[:20]:
        logger.info("[source] %s | %d link(s) | %s", page.page_id, changed, page.title[:80])
    for page, english in english_title_updates[:20]:
        logger.info("[english] %s | %s -> %s", page.page_id, page.title[:60], english[:80])

    if apply:
        for page in garbage + duplicate_archives:
            notion.pages.update(page_id=page.page_id, archived=True)
        for page, updated_sources, _ in source_updates:
            notion.pages.update(
                page_id=page.page_id,
                properties={SOURCES_PROP: {"rich_text": _sources_rich_text_from_markdown(updated_sources)}},
            )
        for page in external_id_updates:
            notion.pages.update(page_id=page.page_id, properties=_external_id_property(page))
        for page, english in english_title_updates:
            notion.pages.update(
                page_id=page.page_id,
                properties=_english_title_property(english),
            )
        logger.info("Applied cleanup changes.")
    else:
        logger.info("Dry-run only. Re-run with --apply to archive/update Notion.")

    return {
        "pages": len(pages),
        "garbage": len(garbage),
        "duplicates": len(duplicate_archives),
        "source_updates": len(source_updates),
        "external_id_updates": len(external_id_updates),
        "english_title_updates": len(english_title_updates),
        "applied": apply,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit and clean ESG Notion database")
    parser.add_argument("--database-id", default=os.environ.get("NOTION_DATABASE_ID", ""))
    parser.add_argument("--token", default=os.environ.get("NOTION_TOKEN", ""))
    parser.add_argument("--apply", action="store_true", help="Apply archive/update actions. Default is dry-run.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.token or not args.database_id:
        raise SystemExit("NOTION_TOKEN and NOTION_DATABASE_ID are required.")
    notion = NotionClient(auth=args.token)
    clean_database(notion, args.database_id, apply=args.apply)


if __name__ == "__main__":
    main()

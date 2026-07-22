"""
notion_upsert.py — 幂等 Notion 写入助手
═══════════════════════════════════════════════════════════════════════════════
通过持久化的 External ID 实现幂等 upsert，彻底消除重复条目。

工作流程：
    1. 对每条事件计算确定性 External ID: hash(entity|date|title)
    2. 在 Notion 数据库中查询 External ID 匹配的页面
    3. 若存在 → 更新该页面（保留手动编辑的字段）
    4. 若不存在 → 创建新页面，写入 External ID
    5. dry_run=True 时仅打印日志，不执行 API 调用

外部使用：
    from notion_upsert import upsert_notion_page
    upsert_notion_page(event, notion_client, database_id, dry_run=False)
═══════════════════════════════════════════════════════════════════════════════
"""

import logging
import re
from typing import Any, Callable, Optional

from notion_mapping import (
    EXTERNAL_ID,
    map_risk_category,
    map_practice_category,
    map_entity,
)
from esg_agent.canonical import canonical_external_id, canonical_event_key, fallback_english_title
from esg_agent.fetchers import resolve_news_url
from radar_infra.sink import generate_external_id

logger = logging.getLogger("notion_upsert")


def _clean_source_name(name: str) -> str:
    text = str(name or "").strip()
    if not text:
        return ""
    # Drop any pre-attached quality/language suffixes like " (英语)" or " (媒体/英语)"
    text = re.sub(r"\s*\([^)]*(?:英语|中文|媒体|新闻源|官方|聚合)[^)]*\)\s*$", "", text)
    return text.strip()


def _build_sources_rich_text(sources_text: Any) -> list[dict]:
    rich_text: list[dict] = []
    if isinstance(sources_text, list):
        items = []
        for s in sources_text[:8]:
            if isinstance(s, dict):
                name = _clean_source_name(s.get("name", ""))
                url = resolve_news_url(str(s.get("url", "")))
                if name:
                    items.append((name, url if url.startswith("http") else ""))
            elif isinstance(s, str):
                name = _clean_source_name(s)
                if name:
                    items.append((name, ""))
        for idx, (name, url) in enumerate(items):
            if idx:
                rich_text.append({"text": {"content": ", "}})
            rich_text.append({"text": {"content": name, "link": {"url": url}}} if url else {"text": {"content": name}})
        return rich_text

    text = str(sources_text or "").strip()
    if text:
        rich_text.append({"text": {"content": text[:2000]}})
    return rich_text


def query_page_by_external_id(
    notion: Any,
    database_id: str,
    external_id: str,
) -> Optional[str]:
    """
    在 Notion 数据库中查询 External ID 匹配的页面。

    Args:
        notion: NotionClient 实例
        database_id: Notion 数据库 ID
        external_id: 要匹配的 External ID 值

    Returns:
        匹配页面的 page_id，或 None 表示不存在
    """
    # 尝试通过 databases.query 端点查询（最兼容 SDK 及 Mock 场景）
    try:
        if hasattr(notion, "databases") and hasattr(notion.databases, "query"):
            result = notion.databases.query(
                database_id=database_id,
                filter={
                    "property": EXTERNAL_ID,
                    "rich_text": {"equals": external_id},
                },
                page_size=1,
            )
            if isinstance(result, dict):
                pages = result.get("results", [])
                if pages:
                    return pages[0]["id"]
    except Exception:
        pass

    # 尝试通过 data_sources 端点查询（inline database 场景）
    try:
        if hasattr(notion, "databases") and hasattr(notion.databases, "retrieve"):
            db_info = notion.databases.retrieve(database_id=database_id)
            if isinstance(db_info, dict):
                ds_list = db_info.get("data_sources", [])
                if ds_list and hasattr(notion, "data_sources"):
                    data_source_id = ds_list[0].get("id")
                    result = notion.data_sources.query(
                        data_source_id=data_source_id,
                        filter={
                            "property": EXTERNAL_ID,
                            "rich_text": {"equals": external_id},
                        },
                        page_size=1,
                    )
                    if isinstance(result, dict):
                        pages = result.get("results", [])
                        if pages:
                            return pages[0]["id"]
    except Exception:
        pass

    # 回退尝试 notion.request 通用 HTTP 请求
    try:
        if hasattr(notion, "request"):
            result = notion.request(
                path=f"databases/{database_id}/query",
                method="POST",
                body={
                    "filter": {
                        "property": EXTERNAL_ID,
                        "rich_text": {"equals": external_id},
                    },
                    "page_size": 1,
                },
            )
            if isinstance(result, dict):
                pages = result.get("results", [])
                if pages:
                    return pages[0]["id"]
    except Exception:
        pass

    return None


def query_page_by_event_key(
    notion: Any,
    database_id: str,
    event: dict,
) -> Optional[str]:
    """
    当 External ID 未查到时，以 Entity + Date + Canonical Event Key 作为二级兜底过滤，
    防范大模型生成的不同字眼标题产生重复页面。
    """
    entity = map_entity(event.get("entity", ""))
    date = str(event.get("date", ""))[:10]
    if not entity or not date or entity == "其他":
        return None

    filter_body = {
        "and": [
            {"property": "Entity", "select": {"equals": entity}},
            {"property": "Date", "date": {"equals": date}},
        ]
    }

    try:
        pages = []
        if hasattr(notion, "databases") and hasattr(notion.databases, "query"):
            res = notion.databases.query(database_id=database_id, filter=filter_body, page_size=20)
            if isinstance(res, dict):
                pages = res.get("results", [])
        elif hasattr(notion, "request"):
            res = notion.request(
                path=f"databases/{database_id}/query",
                method="POST",
                body={"filter": filter_body, "page_size": 20},
            )
            if isinstance(res, dict):
                pages = res.get("results", [])

        target_key = canonical_event_key(event)
        for p in pages:
            props = p.get("properties", {})
            p_title = "".join(t.get("plain_text", "") for t in props.get("标题", {}).get("title", []))
            p_event = {"entity": entity, "date": date, "display_title_zh": p_title}
            if canonical_event_key(p_event) == target_key:
                return p.get("id")
    except Exception:
        pass

    return None


def build_notion_properties(event: dict) -> dict:
    """
    将事件字典映射为 Notion API 的 properties 格式（含分类映射和 External ID）。

    这是 backfill_notion.py 和 esg_intelligence_agent.py 共享的统一映射函数。
    """
    mapped_category = map_risk_category(
        event.get("category", event.get("risk_category", "")),
        event.get("display_title_zh", ""),
        event.get("insight", event.get("executive_insight", "")),
    )

    # 标题：兼容两种字段名
    title_text = event.get("display_title_zh", "")[:2000]
    english_title = fallback_english_title(event)[:2000]
    insight_text = event.get("insight", event.get("executive_insight", ""))[:2000]
    sources_text = event.get("sources", "")
    sources_rich_text = _build_sources_rich_text(sources_text)

    return {
        "标题": {
            "title": [{"text": {"content": title_text}}]
        },
        "English Title": {
            "rich_text": [{"text": {"content": english_title}}]
        },
        "Entity": {
            "select": {"name": map_entity(event.get("entity", ""))}
        },
        "Risk Category": {
            "select": {"name": mapped_category}
        },
        "Date": {
            "date": {"start": event.get("date", "")}
        },
        "Executive Insight": {
            "rich_text": [{"text": {"content": insight_text}}]
        },
        "Sources": {"rich_text": sources_rich_text},
        "Materiality": {
            "select": {"name": event.get("materiality", "🔴 直接材料冲击")}
        },
        "Mode": {
            "select": {"name": event.get("mode", "")}
        },
        "Push Date": {
            "date": {"start": event.get("push_date", "")}
        },
        EXTERNAL_ID: {
            "rich_text": [{"text": {"content": event.get("_external_id", "")}}]
        },
    }


def upsert_notion_page(
    event: dict,
    notion: Any,
    database_id: str,
    dry_run: bool = False,
    properties_builder: Optional[Callable[[dict], dict]] = None,
) -> tuple[str, Optional[str]]:
    """
    幂等地将事件写入 Notion 数据库。

    Args:
        event: 事件字典（需包含 entity, date, display_title_zh）
        notion: NotionClient 实例
        database_id: Notion 数据库 ID
        dry_run: True 时仅打印日志，不执行 API 调用
        properties_builder: 可选的 properties 构建函数，默认 build_notion_properties。
            practice 轨道传入 build_practice_properties。

    Returns:
        (action, page_id) — action 为 "created" / "updated" / "skipped"
    """
    # 强制执行实体归一化，从源头上杜绝 Entity 变成 "其他"
    original_entity = event.get("entity", "")
    canonical_entity_name = map_entity(original_entity)
    event["entity"] = canonical_entity_name

    entity = canonical_entity_name
    date = event.get("date", "")
    title = event.get("display_title_zh", "")
    title_short = title[:50]

    external_id = canonical_external_id(event)
    event["_canonical_event_key"] = canonical_event_key(event)
    event["_external_id"] = external_id

    if dry_run:
        logger.info(
            f"  [DRY-RUN] External ID={external_id} | "
            f"key={event['_canonical_event_key']} | {entity} | {title_short}..."
        )
        return ("skipped", None)

    # 1. 一级防线：通过精准 External ID 查询
    existing_page_id = query_page_by_external_id(notion, database_id, external_id)

    # 2. 二级防线：若 External ID 匹配失败，通过 Entity + Date + Event Key 语义匹配防重
    if not existing_page_id:
        existing_page_id = query_page_by_event_key(notion, database_id, event)
        if existing_page_id:
            logger.info(
                f"  🛡️ 二级语义防重关卡命中匹配页面 [{existing_page_id[:8]}]，将执行更新升级而非重复创建。"
            )

    if properties_builder is None:
        properties_builder = build_notion_properties
    properties = properties_builder(event)

    if existing_page_id:
        # 更新已有页面
        notion.pages.update(
            page_id=existing_page_id,
            properties=properties,
        )
        logger.info(
            f"  🔄 已更新 | External ID={external_id} | {entity} | {title_short}..."
        )
        return ("updated", existing_page_id)
    else:
        # 创建新页面
        new_page = notion.pages.create(
            parent={"database_id": database_id},
            properties=properties,
        )
        logger.info(
            f"  ✅ 已创建 | External ID={external_id} | {entity} | {title_short}..."
        )
        return ("created", new_page.get("id"))


# ════════════════════════════════════════════════════════════════════════════
# Practice 轨道（同业良好实践）— 独立 schema，复用幂等机制
# ════════════════════════════════════════════════════════════════════════════

def build_practice_properties(event: dict) -> dict:
    """
    将实践事件字典映射为 Notion API properties（practice 独立 schema）。

    与 build_notion_properties 的差异：
      - Practice Category（替代 Risk Category）
      - Learning Insight（替代 Executive Insight）
      - Replicable（checkbox，华友可借鉴度，替代 is_direct_material_impact）
      - 其余字段（标题/Entity/Date/Sources/Mode/Push Date/External ID）一致
    """
    mapped_category = map_practice_category(
        event.get("practice_category", event.get("category", ""))
    )

    title_text = event.get("display_title_zh", "")[:2000]
    english_title = fallback_english_title(event)[:2000]
    learning_text = event.get("learning_insight", event.get("insight", ""))[:2000]
    sources_text = event.get("sources", "")
    sources_rich_text = _build_sources_rich_text(sources_text)

    is_replicable = bool(event.get("is_replicable", False))

    return {
        "标题": {
            "title": [{"text": {"content": title_text}}]
        },
        "English Title": {
            "rich_text": [{"text": {"content": english_title}}]
        },
        "Entity": {
            "select": {"name": map_entity(event.get("entity", ""))}
        },
        "Practice Category": {
            "select": {"name": mapped_category}
        },
        "Date": {
            "date": {"start": event.get("date", "")}
        },
        "Learning Insight": {
            "rich_text": [{"text": {"content": learning_text}}]
        },
        "Replicable": {
            "checkbox": is_replicable
        },
        "Sources": {"rich_text": sources_rich_text},
        "Mode": {
            "select": {"name": event.get("mode", "practice")}
        },
        "Push Date": {
            "date": {"start": event.get("push_date", "")}
        },
        EXTERNAL_ID: {
            "rich_text": [{"text": {"content": event.get("_external_id", "")}}]
        },
    }


def upsert_practice_page(
    event: dict,
    notion: Any,
    database_id: str,
    dry_run: bool = False,
) -> tuple[str, Optional[str]]:
    """
    幂等地将实践事件写入 practice 独立 Notion 数据库。

    复用 upsert_notion_page 的幂等核心，仅替换 properties builder。

    Args:
        event: 实践事件字典
        notion: NotionClient 实例
        database_id: practice Notion 数据库 ID
        dry_run: True 时仅打印日志

    Returns:
        (action, page_id) — action 为 "created" / "updated" / "skipped"
    """
    return upsert_notion_page(
        event,
        notion,
        database_id,
        dry_run=dry_run,
        properties_builder=build_practice_properties,
    )

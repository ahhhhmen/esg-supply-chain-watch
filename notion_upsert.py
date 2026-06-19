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

import hashlib
import logging
from typing import Any, Optional

from notion_mapping import EXTERNAL_ID, map_risk_category, map_entity

logger = logging.getLogger("notion_upsert")


def generate_external_id(entity: str, date: str, title: str) -> str:
    """
    为事件生成确定性 External ID。

    使用 SHA-256 前 12 位十六进制字符，碰撞概率 < 10^-9。

    Args:
        entity: 企业名称
        date: 事件日期 (YYYY-MM-DD)
        title: 事件标题

    Returns:
        12 位十六进制 External ID 字符串
    """
    raw = f"{entity}|{date}|{title}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


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
    # 尝试通过 data_sources 端点查询（inline database 场景）
    try:
        db_info = notion.databases.retrieve(database_id=database_id)
        ds_list = db_info.get("data_sources", [])
        if ds_list:
            data_source_id = ds_list[0].get("id")
            result = notion.data_sources.query(
                data_source_id=data_source_id,
                filter={
                    "property": EXTERNAL_ID,
                    "rich_text": {"equals": external_id},
                },
                page_size=1,
            )
            pages = result.get("results", [])
            if pages:
                return pages[0]["id"]
    except Exception:
        pass

    # 回退到 databases.query 端点
    try:
        result = notion.databases.query(
            database_id=database_id,
            filter={
                "property": EXTERNAL_ID,
                "rich_text": {"equals": external_id},
            },
            page_size=1,
        )
        pages = result.get("results", [])
        if pages:
            return pages[0]["id"]
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
    english_title = event.get("english_title", event.get("core_event_title_en", ""))[:2000]
    insight_text = event.get("insight", event.get("executive_insight", ""))[:2000]
    sources_text = event.get("sources", "")

    # sources 可能已经是格式化字符串，也可能是列表
    if isinstance(sources_text, list):
        src_parts = []
        for s in sources_text[:5]:
            if isinstance(s, dict):
                name = s.get("name", "")
                url = s.get("url", "")
                if url:
                    src_parts.append(f"[{name}]({url})")
                else:
                    src_parts.append(name)
            else:
                src_parts.append(str(s))
        sources_text = ", ".join(src_parts)
    sources_text = sources_text[:2000] if isinstance(sources_text, str) else ""

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
        "Sources": {
            "rich_text": [{"text": {"content": sources_text}}]
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
) -> tuple[str, Optional[str]]:
    """
    幂等地将事件写入 Notion 数据库。

    Args:
        event: 事件字典（需包含 entity, date, display_title_zh）
        notion: NotionClient 实例
        database_id: Notion 数据库 ID
        dry_run: True 时仅打印日志，不执行 API 调用

    Returns:
        (action, page_id) — action 为 "created" / "updated" / "skipped"
    """
    entity = event.get("entity", "")
    date = event.get("date", "")
    title = event.get("display_title_zh", "")
    title_short = title[:50]

    external_id = generate_external_id(entity, date, title)
    event["_external_id"] = external_id

    if dry_run:
        logger.info(f"  [DRY-RUN] External ID={external_id} | {entity} | {title_short}...")
        return ("skipped", None)

    # 查询是否已存在
    existing_page_id = query_page_by_external_id(notion, database_id, external_id)

    properties = build_notion_properties(event)

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

#!/usr/bin/env python3
"""
backfill_notion.py — 将 git 历史中的 ESG 报告回填到 Notion 数据库
═══════════════════════════════════════════════════════════════════════════════
用法：
    # 1. 先设置环境变量
    export NOTION_TOKEN="ntn_xxx..."
    export NOTION_DATABASE_ID="xxx..."

    # 2. 干跑模式 — 仅解析打印，不写入 Notion
    python backfill_notion.py --dry-run

    # 3. 正式回填
    python backfill_notion.py

    # 4. 限定回填条数
    python backfill_notion.py --limit 50

    # 5. 从指定 commit 开始回填
    python backfill_notion.py --since "2026-05-20"
═══════════════════════════════════════════════════════════════════════════════
"""

import argparse
import json
import logging
import os
import re
import subprocess
from datetime import datetime
from typing import Optional

from notion_client import Client as NotionClient
from notion_mapping import map_risk_category, map_entity

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_notion")

# ── 风险分类关键词映射 ──────────────────────────────────

CATEGORY_PATTERNS = [
    (r"供应链断裂预警", "供应链断裂预警"),
    (r"政策与市场准入", "政策与市场准入"),
    (r"合规与运营危机", "合规与运营危机"),
    (r"早期合规预警", "早期合规预警"),
    (r"机构与声誉预警", "机构与声誉预警"),
]

# ── 报告模式识别 ──────────────────────────────────────────

MODE_PATTERNS = [
    (r"Daily Intelligence", "daily"),
    (r"日报", "daily"),
    (r"舆情简报", "daily"),
    (r"Weekly", "weekly"),
    (r"周报", "weekly"),
    (r"地缘政策周报", "weekly"),
]


def extract_mode_from_title(title: str) -> str:
    """从报告标题推断运行模式。"""
    for pattern, mode in MODE_PATTERNS:
        if re.search(pattern, title):
            return mode
    return "daily"


def extract_category_from_section(section_title: str) -> str:
    """从章节标题提取风险分类。"""
    for pattern, category in CATEGORY_PATTERNS:
        if re.search(pattern, section_title):
            return category
    return "未知分类"


def parse_report(md_text: str, commit_date: str, commit_hash: str) -> list[dict]:
    """
    从 Markdown 报告中解析事件列表。

    支持两种格式：
    1. **企业 | 标题**（加粗标题行）
    2. 💡 洞察文本
    3. 📅 日期 | 📰 信息源

    返回事件字典列表。
    """
    events = []

    # 识别报告标题以推断 mode
    first_line = md_text.strip().split("\n")[0] if md_text.strip() else ""
    mode = extract_mode_from_title(first_line)

    # 按 "## " 分割为章节（包括 【xxx】 和 🔍盲区分析 等格式）
    sections = re.split(r"\n##\s+", md_text)

    for section in sections:
        if not section.strip():
            continue

        # 提取章节标题（第一行）
        section_lines = section.strip().split("\n")
        section_title = section_lines[0].strip()

        # 仅保留事件章节：包含 【xxx预警/政策/危机/合规】 格式的标题
        if not re.search(r"【.*?(?:预警|政策|危机|合规).*?】", section_title):
            continue
        # 跳过监控矩阵盲区分析
        if "盲区" in section_title:
            continue

        category = extract_category_from_section(section_title)

        # 按 "---" 分割事件块（事件之间用分隔线隔开）
        # 去掉章节引言行（> 仅限...）
        section_body = "\n".join(section_lines[1:])
        event_blocks = re.split(r"\n---\s*\n", section_body)

        for block in event_blocks:
            event = parse_event_block(block, category, mode, commit_date, commit_hash)
            if event:
                events.append(event)

    return events


def parse_event_block(block: str, category: str, mode: str,
                      commit_date: str, commit_hash: str) -> Optional[dict]:
    """
    从单个事件块中提取字段。

    事件块格式示例：
        **大众汽车 | Volkswagen | 大众汽车宣布在德国裁员1.9万人...**

        💡 高管洞察：大众汽车在德国裁员...

        📅 2026-06-11 | 📰 信息源聚合：[qz.com (英语)](url), ...
    """
    block = block.strip()
    if not block or not re.search(r"\*\*.*\*\*", block):
        return None

    event: dict = {
        "category": category,
        "mode": mode,
        "push_date": commit_date,
    }

    # 提取标题行 (**...**)
    title_match = re.search(r"\*\*(.+?)\*\*\s*\n", block)
    if title_match:
        raw_title = title_match.group(1).strip()
        # 分割实体和标题 — 格式: "企业 | 英文名 | 事件标题" 或 "企业 | 事件标题"
        parts = [p.strip() for p in raw_title.split("|")]
        if len(parts) >= 3:
            event["entity"] = parts[0]
            # parts[1] 是英文名
            event["english_title"] = parts[1]
            event["display_title_zh"] = " | ".join(parts[2:])
        elif len(parts) == 2:
            event["entity"] = parts[0]
            event["english_title"] = ""
            event["display_title_zh"] = parts[1]
        else:
            event["entity"] = "未知"
            event["english_title"] = ""
            event["display_title_zh"] = raw_title
    else:
        return None

    # 提取洞察 (💡 ...)
    insight_match = re.search(r"💡[^\n：:]*[:：]\s*(.+?)(?=\n📅|\n---|\Z)", block, re.DOTALL)
    if insight_match:
        event["insight"] = insight_match.group(1).strip().replace("\n", " ")
    else:
        event["insight"] = ""

    # 提取日期 (📅 YYYY-MM-DD)
    date_match = re.search(r"📅\s*(\d{4}-\d{2}-\d{2})", block)
    if date_match:
        event["date"] = date_match.group(1)
    else:
        event["date"] = commit_date

    # 提取来源 (📰 ...)
    sources_match = re.search(r"📰[^\n：:]*[:：]\s*(.+?)(?=\n---|\n\n|\Z)", block, re.DOTALL)
    if sources_match:
        sources_text = sources_match.group(1).strip().replace("\n", " ")
        # 提取 Markdown 链接中的来源名称
        source_names = re.findall(r"\[([^\]]+)\]", sources_text)
        if source_names:
            event["sources"] = ", ".join(source_names)
        else:
            event["sources"] = re.sub(r"\[.*?\]\(.*?\)", "", sources_text).strip()
    else:
        event["sources"] = ""

    return event


def get_report_commits(since: Optional[str] = None) -> list[dict]:
    """
    从 git 历史中获取包含 esg_global_report.md 更新的 commit 列表。
    返回 [{"hash": str, "date": str, "message": str}, ...]
    """
    cmd = [
        "git", "log", "--all", "--oneline", "--diff-filter=AM",
        "--", "esg_global_report.md"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=".")

    if result.returncode != 0:
        logger.error(f"git log 失败: {result.stderr}")
        return []

    commits = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.strip().split(" ", 1)
        commit_hash = parts[0]
        message = parts[1] if len(parts) > 1 else ""

        # 获取 commit 日期
        date_cmd = ["git", "show", "-s", "--format=%cs", commit_hash]
        date_result = subprocess.run(date_cmd, capture_output=True, text=True, cwd=".")
        commit_date = date_result.stdout.strip() if date_result.returncode == 0 else ""

        if since and commit_date < since:
            continue

        commits.append({
            "hash": commit_hash,
            "date": commit_date,
            "message": message,
        })

    return commits


def get_report_content(commit_hash: str) -> str:
    """获取指定 commit 中的 esg_global_report.md 内容。"""
    cmd = ["git", "show", f"{commit_hash}:esg_global_report.md"]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=".")
    if result.returncode == 0:
        return result.stdout
    return ""


def build_notion_properties(event: dict) -> dict:
    """将事件字典映射为 Notion API 的 properties 格式（含分类映射）。"""
    mapped_category = map_risk_category(
        event.get("category", ""),
        event.get("display_title_zh", ""),
        event.get("insight", ""),
    )
    return {
        "标题": {
            "title": [{"text": {"content": event.get("display_title_zh", "")[:2000]}}]
        },
        "English Title": {
            "rich_text": [{"text": {"content": event.get("english_title", "")[:2000]}}]
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
            "rich_text": [{"text": {"content": event.get("insight", "")[:2000]}}]
        },
        "Sources": {
            "rich_text": [{"text": {"content": event.get("sources", "")[:2000]}}]
        },
        "Mode": {
            "select": {"name": event.get("mode", "")}
        },
        "Push Date": {
            "date": {"start": event.get("push_date", "")}
        },
    }


def dedup_key(event: dict) -> str:
    """生成去重键 — 基于企业+日期+标题。"""
    return f"{event.get('entity', '')}|{event.get('date', '')}|{event.get('display_title_zh', '')[:50]}"


def write_to_notion(events: list[dict], database_id: str, token: str,
                    limit: Optional[int] = None) -> tuple[int, int]:
    """
    将事件列表写入 Notion 数据库。
    返回 (成功数, 失败数)。
    """
    notion = NotionClient(auth=token)

    # 获取已有页面用于去重（通过查询数据库）
    # 兼容新版 Notion SDK：优先用 data_sources.query，回退到 databases.query
    existing_keys = set()
    try:
        has_more = True
        start_cursor = None

        # 尝试找到对应的 data_source_id（inline database 需要通过 data_sources 端点查询）
        data_source_id = None
        try:
            db_info = notion.databases.retrieve(database_id=database_id)
            ds_list = db_info.get("data_sources", [])
            if ds_list:
                data_source_id = ds_list[0].get("id")
        except Exception:
            pass

        while has_more:
            if data_source_id:
                result = notion.data_sources.query(
                    data_source_id=data_source_id,
                    page_size=100,
                    **({"start_cursor": start_cursor} if start_cursor else {}),
                )
            else:
                query_params = {
                    "database_id": database_id,
                    "page_size": 100,
                }
                if start_cursor:
                    query_params["start_cursor"] = start_cursor
                result = notion.databases.query(**query_params)

            for page in result.get("results", []):
                props = page.get("properties", {})
                # 构建去重键
                entity = ""
                if "Entity" in props and props["Entity"].get("select"):
                    entity = props["Entity"]["select"].get("name", "")
                date = ""
                if "Date" in props and props["Date"].get("date"):
                    date = props["Date"]["date"].get("start", "")
                title = ""
                if "标题" in props and props["标题"].get("title"):
                    title = props["标题"]["title"][0]["plain_text"][:50] if props["标题"]["title"] else ""
                existing_keys.add(f"{entity}|{date}|{title}")

            has_more = result.get("has_more", False)
            start_cursor = result.get("next_cursor")
    except Exception as e:
        logger.warning(f"查询已有 Notion 记录失败（将跳过去重）: {e}")

    # 去重
    new_events = []
    for event in events:
        key = dedup_key(event)
        if key not in existing_keys:
            new_events.append(event)
        else:
            logger.debug(f"跳过已存在事件: {key}")

    if limit:
        new_events = new_events[:limit]

    logger.info(f"待写入事件: {len(new_events)} 条（去重后）")

    success_count = 0
    fail_count = 0

    for i, event in enumerate(new_events):
        try:
            properties = build_notion_properties(event)
            notion.pages.create(
                parent={"database_id": database_id},
                properties=properties,
            )
            success_count += 1
            logger.info(
                f"  [{i + 1}/{len(new_events)}] ✅ {event.get('entity', '?')} | "
                f"{event.get('display_title_zh', '?')[:40]}"
            )
        except Exception as e:
            fail_count += 1
            logger.warning(
                f"  [{i + 1}/{len(new_events)}] ❌ {event.get('entity', '?')} | "
                f"{event.get('display_title_zh', '?')[:40]}: {e}"
            )

    return success_count, fail_count


def main():
    parser = argparse.ArgumentParser(
        description="backfill_notion — 将 git 历史中的 ESG 报告回填到 Notion 数据库",
    )
    parser.add_argument("--dry-run", action="store_true", help="仅解析打印，不写入 Notion")
    parser.add_argument("--limit", type=int, default=None, help="最多回填条数")
    parser.add_argument("--since", type=str, default=None,
                        help="从该日期开始的 commit 才回填 (YYYY-MM-DD)")
    parser.add_argument("--database-id", type=str, default=None, help="Notion Database ID（也可用环境变量）")
    parser.add_argument("--token", type=str, default=None, help="Notion Token（也可用环境变量）")
    args = parser.parse_args()

    token = args.token or os.environ.get("NOTION_TOKEN", "")
    database_id = args.database_id or os.environ.get("NOTION_DATABASE_ID", "")

    if not args.dry_run and (not token or not database_id):
        logger.error("请设置 NOTION_TOKEN 和 NOTION_DATABASE_ID 环境变量，或通过参数传入。")
        logger.error("  export NOTION_TOKEN='ntn_xxx...'")
        logger.error("  export NOTION_DATABASE_ID='xxx...'")
        return

    # 1. 获取包含报告的 commit 列表
    logger.info("正在从 git 历史中提取报告 commit...")
    commits = get_report_commits(since=args.since)
    logger.info(f"找到 {len(commits)} 个包含报告更新的 commit")

    if not commits:
        logger.info("没有找到可回填的报告。")
        return

    # 2. 解析每个 commit 中的报告
    all_events: list[dict] = []
    for commit in commits:
        content = get_report_content(commit["hash"])
        if not content:
            logger.warning(f"  commit {commit['hash'][:8]} 无报告内容，跳过")
            continue

        events = parse_report(content, commit_date=commit["date"], commit_hash=commit["hash"])
        if events:
            logger.info(f"  commit {commit['hash'][:8]} ({commit['date']}) → {len(events)} 条事件")
            all_events.extend(events)
        else:
            logger.info(f"  commit {commit['hash'][:8]} ({commit['date']}) → 无事件（无风险日）")

    logger.info(f"共解析出 {len(all_events)} 条事件")

    if not all_events:
        logger.info("无事件可回填。")
        return

    # 3. 打印摘要（始终打印）
    logger.info("═══ 回填事件摘要 ═══")
    for i, event in enumerate(all_events[:args.limit] if args.limit else all_events):
        logger.info(
            f"  [{i + 1}] {event.get('date', '?')} | "
            f"{event.get('mode', '?')} | "
            f"{event.get('category', '?')} | "
            f"{event.get('entity', '?')} | "
            f"{event.get('display_title_zh', '?')[:50]}"
        )

    if args.dry_run:
        logger.info(f"\n🔍 干跑模式 — 以上 {len(all_events)} 条事件将被写入 Notion（实际未写入）。")
        return

    # 4. 写入 Notion
    logger.info(f"\n开始向 Notion 写入 {len(all_events)} 条事件...")
    success, fail = write_to_notion(all_events, database_id, token, limit=args.limit)
    logger.info(f"\n═══ 回填完成: ✅ {success} 成功 / ❌ {fail} 失败 ═══")


if __name__ == "__main__":
    main()

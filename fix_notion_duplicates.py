#!/usr/bin/env python3
"""
fix_notion_duplicates.py — 修复 Notion 数据库中的重复、旧算法 External ID 与 Entity '其他' 问题
"""

from __future__ import annotations

import os
import sys
import dotenv
import requests

from audit_notion_duplicates import fetch_all_pages_http, parse_page, DB_RISK_ID, DB_PRACTICE_ID
from notion_mapping import map_entity, EXTERNAL_ID
from esg_agent.canonical import canonical_external_id

dotenv.load_dotenv()
TOKEN = os.environ.get("NOTION_TOKEN")
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def update_page_properties(page_id: str, properties: dict):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    resp = requests.patch(url, headers=HEADERS, json={"properties": properties}, timeout=15)
    if resp.status_code == 200:
        print(f"   ✅ 已更新页面 [{page_id[:8]}] 属性")
    else:
        print(f"   ❌ 更新页面 [{page_id[:8]}] 失败: {resp.status_code} - {resp.text}")


def archive_page(page_id: str):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    resp = requests.patch(url, headers=HEADERS, json={"archived": True}, timeout=15)
    if resp.status_code == 200:
        print(f"   🗑️ 已归档重复页面 [{page_id[:8]}]")
    else:
        print(f"   ❌ 归档页面 [{page_id[:8]}] 失败: {resp.status_code} - {resp.text}")


def fix_database(db_id: str, db_name: str):
    print(f"\n==========================================")
    print(f"🛠️ 开始修复 [{db_name}]")
    print(f"==========================================")

    raw_pages = fetch_all_pages_http(db_id, db_name)
    records = [parse_page(p, db_name) for p in raw_pages]

    # 1. 修复 Entity == "其他"
    print("\n1️⃣ 纠正 Entity 为 '其他' 的条目...")
    for r in records:
        if r["entity"] == "其他" or not r["entity"]:
            corrected_entity = map_entity(r["title"])
            if corrected_entity and corrected_entity != "其他":
                print(f"   -> 将 [{r['page_id'][:8]}] 实体由 '其他' 更新为 '{corrected_entity}' (标题: {r['title'][:30]})")
                update_page_properties(
                    r["page_id"],
                    {"Entity": {"select": {"name": corrected_entity}}}
                )
                r["entity"] = corrected_entity
                r["calc_external_id"] = canonical_external_id({
                    "entity": corrected_entity,
                    "date": r["date"],
                    "display_title_zh": r["title"]
                })

    # 2. 批量升级旧 External ID
    print("\n2️⃣ 升级 External ID 至最新 SHA-256 确定性算法...")
    stale_count = 0
    for r in records:
        if r["calc_external_id"] and r["external_id"] != r["calc_external_id"]:
            stale_count += 1
            print(f"   -> 页面 [{r['page_id'][:8]}] ExtID 升级: {r['external_id']} => {r['calc_external_id']}")
            update_page_properties(
                r["page_id"],
                {EXTERNAL_ID: {"rich_text": [{"text": {"content": r["calc_external_id"]}}]}}
            )
    print(f"   共升级 {stale_count} 条 External ID。")

    # 3. 处理语义重复项
    print("\n3️⃣ 处理 Canonical Event Key 语义重复页面...")
    key_groups = {}
    for r in records:
        if r["calc_event_key"]:
            key_groups.setdefault(r["calc_event_key"], []).append(r)

    archived_dups = 0
    for key, group in key_groups.items():
        if len(group) > 1:
            print(f"   -> 发现重复组 Event Key [{key}]，共有 {len(group)} 条记录:")
            # 保留创建时间更早或来源更丰富的页面，归档其余页面
            group_sorted = sorted(group, key=lambda x: (x["last_edited_time"], -len(x["sources"])))
            canonical_page = group_sorted[0]
            print(f"      正本保留: [{canonical_page['page_id'][:8]}] {canonical_page['title'][:40]}")
            for dup in group_sorted[1:]:
                print(f"      准备归档重复项: [{dup['page_id'][:8]}] {dup['title'][:40]}")
                archive_page(dup["page_id"])
                archived_dups += 1

    print(f"   共归档 {archived_dups} 条语义重复页面。")


def main():
    if DB_RISK_ID:
        fix_database(DB_RISK_ID, "Risk DB (风险库)")
    if DB_PRACTICE_ID:
        fix_database(DB_PRACTICE_ID, "Practice DB (实践库)")


if __name__ == "__main__":
    main()

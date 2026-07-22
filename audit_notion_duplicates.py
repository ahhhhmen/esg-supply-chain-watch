#!/usr/bin/env python3
"""
audit_notion_duplicates.py — 全量反向检查 Notion 数据库重复与异常条目
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from typing import Any, Dict, List

import dotenv
import requests

from backend.utils.references import clean_title
from esg_agent.canonical import canonical_event_key, canonical_external_id, fallback_english_title
from esg_agent.filters import EntityFilter
from notion_mapping import EXTERNAL_ID

dotenv.load_dotenv()

TOKEN = os.environ.get("NOTION_TOKEN")
DB_RISK_ID = os.environ.get("NOTION_DATABASE_ID")
DB_PRACTICE_ID = os.environ.get("NOTION_PRACTICE_DATABASE_ID")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_NOISE_TITLE_RE = re.compile(
    r"今日无风险事件|无新增风险|系统巡检完成|监控矩阵盲区|token 消耗",
    re.IGNORECASE,
)


def fetch_all_pages_http(database_id: str, db_name: str) -> List[Dict[str, Any]]:
    if not database_id:
        print(f"⚠️ [{db_name}] Database ID 未配置，跳过拉取。")
        return []

    pages: List[Dict[str, Any]] = []
    cursor = None
    url = f"https://api.notion.com/v1/databases/{database_id}/query"

    print(f"🔄 正在拉取 [{db_name}] 全量页面...")
    while True:
        body: Dict[str, Any] = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor

        resp = requests.post(url, headers=HEADERS, json=body, timeout=30)
        if resp.status_code != 200:
            print(f"❌ 拉取 [{db_name}] 失败: {resp.status_code} - {resp.text}")
            break

        data = resp.json()
        results = data.get("results", [])
        pages.extend(results)

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    print(f"✅ [{db_name}] 成功拉取 {len(pages)} 条有效页面。")
    return pages


def _plain_text(value: Dict[str, Any]) -> str:
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


def parse_page(page: Dict[str, Any], db_name: str) -> Dict[str, Any]:
    props = page.get("properties", {})
    title = _plain_text(props.get("标题", {})).strip()
    entity = _plain_text(props.get("Entity", {})).strip()
    date = _plain_text(props.get("Date", {})).strip()[:10]
    sources = _plain_text(props.get("Sources", {})).strip()
    english_title = _plain_text(props.get("English Title", {})).strip()
    external_id = _plain_text(props.get(EXTERNAL_ID, {})).strip()
    materiality = _plain_text(props.get("Materiality", {})).strip()
    mode = _plain_text(props.get("Mode", {})).strip()

    # 提取所有的 URL 链接
    urls = [m.group(2) for m in _MD_LINK_RE.finditer(sources)]

    event_dict = {
        "entity": entity,
        "date": date,
        "display_title_zh": clean_title(title) or title,
        "english_title": english_title,
    }
    calc_event_key = canonical_event_key(event_dict) if entity and date and title else ""
    calc_external_id = canonical_external_id(event_dict) if entity and date and title else ""

    return {
        "page_id": page.get("id", ""),
        "db_name": db_name,
        "title": title,
        "entity": entity,
        "date": date,
        "sources": sources,
        "urls": urls,
        "english_title": english_title,
        "external_id": external_id,
        "materiality": materiality,
        "mode": mode,
        "last_edited_time": page.get("last_edited_time", ""),
        "url": page.get("url", ""),
        "calc_event_key": calc_event_key,
        "calc_external_id": calc_external_id,
        "raw": page,
    }


def analyze_db(records: List[Dict[str, Any]], db_name: str) -> Dict[str, Any]:
    print(f"\n==========================================")
    print(f"📊 开始分析 [{db_name}] （共 {len(records)} 条记录）")
    print(f"==========================================")

    # 1. 垃圾/噪音检查
    garbage_records = []
    for r in records:
        if not r["title"] or not r["entity"] or r["entity"] == "其他":
            garbage_records.append((r, "缺失关键字段 (标题/实体)"))
        elif _NOISE_TITLE_RE.search(r["title"]):
            garbage_records.append((r, "包含巡检/无风险提示标语"))
        elif EntityFilter.is_spam(r["title"], "", r["sources"]):
            garbage_records.append((r, "命中文本垃圾/赌博黑名单"))

    print(f"\n1️⃣ 垃圾/噪音记录: {len(garbage_records)} 条")
    for r, reason in garbage_records[:10]:
        print(f"   ❌ [{r['page_id'][:8]}] [{r['entity']}] {r['title'][:40]} (原因: {reason})")

    # 2. 精确 External ID 重复
    ext_id_map = defaultdict(list)
    missing_ext_id = []
    stale_ext_id = []

    for r in records:
        ext_id = r["external_id"]
        if not ext_id:
            missing_ext_id.append(r)
        else:
            ext_id_map[ext_id].append(r)
            if r["calc_external_id"] and ext_id != r["calc_external_id"]:
                stale_ext_id.append(r)

    exact_ext_dups = {k: v for k, v in ext_id_map.items() if len(v) > 1}
    print(f"\n2️⃣ External ID 维度分析:")
    print(f"   - 缺失 External ID 的页面: {len(missing_ext_id)} 条")
    print(f"   - External ID 与重新计算不一致(旧算法/待更新): {len(stale_ext_id)} 条")
    print(f"   - 精确 External ID 完全相同的重复组: {len(exact_ext_dups)} 组")

    for ext_id, group in list(exact_ext_dups.items())[:10]:
        print(f"\n   🔹 External ID: {ext_id} (共 {len(group)} 条):")
        for r in group:
            print(f"      - PageID: {r['page_id']} | Date: {r['date']} | Title: {r['title'][:50]}")

    # 3. Canonical Event Key 语义重复
    key_map = defaultdict(list)
    for r in records:
        if r["calc_event_key"]:
            key_map[r["calc_event_key"]].append(r)

    canonical_dups = {k: v for k, v in key_map.items() if len(v) > 1}
    print(f"\n3️⃣ Canonical Event Key 语义重复分析:")
    print(f"   - 语义 Key 完全相同的重复组: {len(canonical_dups)} 组")
    for key, group in list(canonical_dups.items())[:10]:
        page_ids = [r["page_id"] for r in group]
        ext_ids = list(set(r["external_id"] for r in group))
        print(f"\n   🔹 Event Key: {key}")
        print(f"      - 涉及 Ext IDs: {ext_ids}")
        for r in group:
            print(f"      - [{r['page_id'][:8]}] [{r['date']}] {r['entity']} | {r['title'][:60]}")

    # 4. URL 新闻直链重复
    url_map = defaultdict(list)
    for r in records:
        for u in r["urls"]:
            if "news.google.com" not in u and len(u) > 15:
                url_map[u].append(r)

    url_dups = {k: v for k, v in url_map.items() if len(v) > 1}
    print(f"\n4️⃣ 新闻直链 (URL) 精确重复分析:")
    print(f"   - 共享相同新闻 URL 的重复组: {len(url_dups)} 组")
    for url, group in list(url_dups.items())[:10]:
        unique_pages = {r["page_id"]: r for r in group}.values()
        if len(unique_pages) > 1:
            print(f"\n   🔹 URL: {url[:80]}...")
            for r in unique_pages:
                print(f"      - [{r['page_id'][:8]}] [{r['date']}] {r['entity']} | {r['title'][:50]}")

    return {
        "total": len(records),
        "garbage": garbage_records,
        "exact_ext_dups": exact_ext_dups,
        "missing_ext_id": missing_ext_id,
        "stale_ext_id": stale_ext_id,
        "canonical_dups": canonical_dups,
        "url_dups": url_dups,
    }


def main():
    risk_raw = fetch_all_pages_http(DB_RISK_ID, "Risk DB (风险库)")
    practice_raw = fetch_all_pages_http(DB_PRACTICE_ID, "Practice DB (实践库)")

    risk_records = [parse_page(p, "Risk DB") for p in risk_raw]
    practice_records = [parse_page(p, "Practice DB") for p in practice_raw]

    risk_res = analyze_db(risk_records, "Risk DB (风险库)")
    practice_res = analyze_db(practice_records, "Practice DB (实践库)")

    # 5. 双库交叉重复校验 (Cross-DB Duplicates)
    print(f"\n==========================================")
    print(f"🔀 开始双库交叉重复排查 (Risk vs Practice)")
    print(f"==========================================")

    risk_keys = {r["calc_event_key"]: r for r in risk_records if r["calc_event_key"]}
    practice_keys = {r["calc_event_key"]: r for r in practice_records if r["calc_event_key"]}

    cross_keys = set(risk_keys.keys()).intersection(set(practice_keys.keys()))
    print(f"   - 跨库 Event Key 冲突组数: {len(cross_keys)}")

    for k in list(cross_keys)[:10]:
        r_item = risk_keys[k]
        p_item = practice_keys[k]
        print(f"\n   ⚠️ 跨库冲突 Event Key: {k}")
        print(f"      - Risk     : [{r_item['page_id'][:8]}] [{r_item['date']}] {r_item['entity']} | {r_item['title'][:50]}")
        print(f"      - Practice : [{p_item['page_id'][:8]}] [{p_item['date']}] {p_item['entity']} | {p_item['title'][:50]}")

    # 总结与统计输出
    print(f"\n==========================================")
    print(f"📋 全量 Notion 数据库排查汇总")
    print(f"==========================================")
    print(f"1️⃣ Risk 库:")
    print(f"   - 总页面数: {risk_res['total']}")
    print(f"   - 垃圾/巡检/测试页面: {len(risk_res['garbage'])}")
    print(f"   - 精确 External ID 重复组: {len(risk_res['exact_ext_dups'])}")
    print(f"   - 语义 Key 重复组: {len(risk_res['canonical_dups'])}")
    print(f"   - 新闻直链 URL 重复组: {len(risk_res['url_dups'])}")
    print(f"   - 缺失 External ID 页面: {len(risk_res['missing_ext_id'])}")
    print(f"   - 需更新 External ID (旧算法版本) 页面: {len(risk_res['stale_ext_id'])}")

    print(f"\n2️⃣ Practice 库:")
    print(f"   - 总页面数: {practice_res['total']}")
    print(f"   - 垃圾/巡检/测试页面: {len(practice_res['garbage'])}")
    print(f"   - 精确 External ID 重复组: {len(practice_res['exact_ext_dups'])}")
    print(f"   - 语义 Key 重复组: {len(practice_res['canonical_dups'])}")
    print(f"   - 新闻直链 URL 重复组: {len(practice_res['url_dups'])}")
    print(f"   - 缺失 External ID 页面: {len(practice_res['missing_ext_id'])}")
    print(f"   - 需更新 External ID (旧算法版本) 页面: {len(practice_res['stale_ext_id'])}")

    print(f"\n3️⃣ 双库交叉重复数: {len(cross_keys)}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
backfill_missing_events.py — 一次性将补全的 3 条 ESG 风险事件写入 Notion 数据库
"""

import os
import sys
import logging
from pathlib import Path
from dotenv import load_dotenv

# 加载 .env 环境变量
env_path = Path("/Users/xiefang/Documents/Projects/esg-supply-chain-watch/.env")
load_dotenv(dotenv_path=env_path)

# 将项目根目录与 radar-infra 放入 Python Path
project_root = Path("/Users/xiefang/Documents/Projects/esg-supply-chain-watch")
radar_infra_path = Path("/Users/xiefang/Documents/Projects/radar-infra/src")
sys.path.insert(0, str(radar_infra_path))
sys.path.insert(0, str(project_root))

from notion_client import Client as NotionClient
from notion_upsert import upsert_notion_page
from esg_agent.canonical import canonical_external_id

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_missing_events")

# 定义要补全的 3 条事件
MISSING_EVENTS = [
    {
        "company_name_zh": "华友钴业",
        "company_name_en": "Huayou Cobalt",
        "company": "华友钴业 | Huayou Cobalt",
        "title": "【历史隐患深度复盘/追责】印尼IMIP园区华越（PT Huayue）尾矿坝溃坝复盘与重金属风险预警",
        "core_event_title_en": "PT Huayue Nickel Cobalt tailings dam collapse and heavy metal spill in IMIP",
        "display_title_zh": "【历史隐患深度复盘/追责】印尼IMIP园区华越（PT Huayue）尾矿坝溃坝复盘与重金属风险预警",
        "risk_category": "合规与运营危机",
        "is_valid_risk": True,
        "is_direct_material_impact": True,
        "date": "2025-03-16",
        "push_date": "2026-07-18",
        "mode": "daily",
        "executive_insight": "华越（PT Huayue）作为华友钴业在印尼 IMIP 园区的湿法镍核心项目，其尾矿设施泄露遭受媒体与 NGO 深度复盘追责，加剧海外投资项目环评与合规审查阻力，并可能对下游客户锂电材料供应链合规认证产生连锁审计风险。",
        "insight": "华越（PT Huayue）作为华友钴业在印尼 IMIP 园区的湿法镍核心项目，其尾矿设施泄露遭受媒体与 NGO 深度复盘追责，加剧海外投资项目环评与合规审查阻力，并可能对下游客户锂电材料供应链合规认证产生连锁审计风险。",
        "sources": [
            {"name": "钛媒体", "url": "https://www.tmtpost.com"},
            {"name": "腾讯新闻", "url": "https://news.qq.com"}
        ],
        "tags": ["【合规与运营危机】", "【印尼HPAL尾矿】"]
    },
    {
        "company_name_zh": "华友钴业",
        "company_name_en": "Huayou Cobalt",
        "company": "华友钴业 | Huayou Cobalt",
        "title": "国际气候权利组织（CRI）报告点名IWIP园区自备燃煤电厂与原住民权益隐患",
        "core_event_title_en": "Climate Rights International report targets IWIP captive coal plants and indigenous land rights",
        "display_title_zh": "国际气候权利组织（CRI）报告点名IWIP园区自备燃煤电厂与原住民权益隐患",
        "risk_category": "机构与声誉预警",
        "is_valid_risk": True,
        "is_direct_material_impact": True,
        "date": "2025-07-10",
        "push_date": "2026-07-18",
        "mode": "daily",
        "executive_insight": "CRI 报告指控华友参与投资的 IWIP 园区存在高碳自备燃煤发电及土地征用争议，直接冲击华友在欧美终端客户（如特斯拉、宝马）的 Responsible Sourcing 负责任采购审视，增加欧盟 CSRD/CSDDD 尽职调查合规审计阻力。",
        "insight": "CRI 报告指控华友参与投资的 IWIP 园区存在高碳自备燃煤发电及土地征用争议，直接冲击华友在欧美终端客户（如特斯拉、宝马）的 Responsible Sourcing 负责任采购审视，增加欧盟 CSRD/CSDDD 尽职调查合规审计阻力。",
        "sources": [
            {"name": "Climate Rights International", "url": "https://cri.org"},
            {"name": "BHRRC", "url": "https://www.business-humanrights.org"}
        ],
        "tags": ["【机构与声誉预警】", "【CRI人权报告】"]
    },
    {
        "company_name_zh": "华友钴业",
        "company_name_en": "Huayou Cobalt",
        "company": "华友钴业 | Huayou Cobalt",
        "title": "华飞镍钴因硫磺价格暴涨及高负荷设备维护实施50%产能临时停产检修",
        "core_event_title_en": "PT Huafei Nickel Cobalt temporary 50% capacity curtailment due to sulfur price surge and maintenance",
        "display_title_zh": "华飞镍钴因硫磺价格暴涨及高负荷设备维护实施50%产能临时停产检修",
        "risk_category": "供应链断裂预警",
        "is_valid_risk": True,
        "is_direct_material_impact": True,
        "date": "2026-05-01",
        "push_date": "2026-07-18",
        "mode": "daily",
        "executive_insight": "华飞 12 万吨 HPAL 项目作为华友镍中间品（MHP）主阵地，50% 产能停产检修将直接导致中游前驱体原材料供给收紧，推升 HPAL 运营成本并可能引发下游正极材料订单交付周期波动。",
        "insight": "华飞 12 万吨 HPAL 项目作为华友镍中间品（MHP）主阵地，50% 产能停产检修将直接导致中游前驱体原材料供给收紧，推升 HPAL 运营成本并可能引发下游正极材料订单交付周期波动。",
        "sources": [
            {"name": "行业研报", "url": "https://news.google.com"}
        ],
        "tags": ["【供应链断裂预警】", "【停产检修】"]
    }
]

def run_backfill(dry_run: bool = False):
    token = os.getenv("NOTION_TOKEN")
    db_id = os.getenv("NOTION_DATABASE_ID")

    if not dry_run and (not token or not db_id):
        logger.error("Missing NOTION_TOKEN or NOTION_DATABASE_ID in environment!")
        sys.exit(1)

    notion_client = NotionClient(auth=token) if (token and not dry_run) else None

    logger.info(f"Starting backfill process (dry_run={dry_run})...")
    for idx, event in enumerate(MISSING_EVENTS, 1):
        # 确保计算确切的 External ID
        ext_id = canonical_external_id(event)
        event["external_id"] = ext_id
        
        logger.info(f"\n[{idx}/3] Event: {event['title']}")
        logger.info(f"      Entity: {event['company']}")
        logger.info(f"      Category: {event['risk_category']}")
        logger.info(f"      External ID: {ext_id}")

        if dry_run:
            logger.info("      [DRY RUN] Would execute upsert_notion_page")
        else:
            action, page_id = upsert_notion_page(
                event=event,
                notion=notion_client,
                database_id=db_id,
                dry_run=False
            )
            logger.info(f"      ✓ Successfully {action} Notion page ID: {page_id}")

    logger.info("\n🎉 All missing events processed successfully!")

if __name__ == "__main__":
    is_dry = "--dry-run" in sys.argv
    run_backfill(dry_run=is_dry)

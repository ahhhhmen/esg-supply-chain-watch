#!/usr/bin/env python3
"""
backfill_raw_urls.py — 针对 7 篇特定的历史舆情 raw Markdown 内容进行 LLM 结构化提取与 Notion 幂等回填。
"""

import os
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List

# 强行加载本地 .env 文件
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ[key.strip()] = val.strip()

# 初始化日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("backfill_raw_urls")

from notion_client import Client as NotionClient
from notion_upsert import upsert_notion_page
from radar_infra.llm import create_provider
from esg_agent.config import AgentConfig
from radar_infra.guard import safe_json_parse

# 7 篇目标文章的配置映射
URL_MAPPING = {
    "url1": {
        "url": "https://www.business-humanrights.org/ru/%D1%81%D0%B2%D0%B5%D0%B6%D0%B8%D0%B5-%D0%BD%D0%BE%D0%B2%D0%BE%D1%81%D1%82%D0%B8/indonesia-iwip-workers-face-heightened-safety-and-health-risks-linked-to-long-working-hours-report-found-cos-did-not-repond/",
        "file": "logs/backfill_raw/url1.md",
        "default_date": "2026-06-10"
    },
    "url2": {
        "url": "https://www.business-humanrights.org/en/latest-news/report-from-long-working-hours-to-sudden-death-investigation-on-deadly-working-hour-in-weda-bay/",
        "file": "logs/backfill_raw/url2.md",
        "default_date": "2025-12-06"
    },
    "url3": {
        "url": "https://www.evidencity.com/china-indonesias-forced-labor-stricken-nickel-parks",
        "file": "logs/backfill_raw/url3.md",
        "default_date": "2025-07-08"
    },
    "url4": {
        "url": "https://visione.co.id/detail/3150/dampak-jam-kerja-panjang-terhadap-kesehatan-mental-pekerja-di-kawasan-industri-iwip",
        "file": "logs/backfill_raw/url4.md",
        "default_date": "2026-05-16"
    },
    "url5": {
        "url": "https://mongabay.co.id/2026/05/01/laporan-sebut-fenomena-bunuh-diri-sampai-mati-mendadak-buruh-di-kawasan-nikel-teluk-weda/",
        "file": "logs/backfill_raw/url5.md",
        "default_date": "2026-05-01"
    },
    "url6": {
        "url": "https://cri.org/indonesia-nickel-industry-harming-human-rights-and-the-environment/",
        "file": "logs/backfill_raw/url6.md",
        "default_date": "2024-01-17"
    },
    "url7": {
        "url": "https://sembadabersama.org/jam-kerjanya-lama-matinya-tiba-tiba-investigasi-kondisi-kerja-mematikan-di-teluk-weda/",
        "file": "logs/backfill_raw/url7.md",
        "default_date": "2025-12-06"
    }
}

SYSTEM_PROMPT = """# Role: ESG 供应链风险数据提取引擎 (历史回填模式)
今天是 2026-07-14。注意：当前任务是历史舆情回填任务。你必须忽略所有关于“文章发布时间太旧”的过滤规则。所有提取出的事件均应判定为有效风险（is_valid_risk 设为 true）。

# Objective
从用户提供的单篇历史新闻正文 Markdown 中，清洗并提取出符合规范 of ESG 风险事件。

# Target Entities
重点监控对象及其关联企业：
  - 华友钴业 | Huayou Cobalt (603799.SS)
  - 青山控股 | Tsingshan Holding (PRIVATE)
  - 特斯拉 | Tesla (TSLA)
  - 宁德时代 | CATL (300750.SZ)
  - 宝马 | BMW (BMW.DE)
  - 梅赛德斯-奔驰 | Mercedes-Benz (MBG.DE)
  - 大众汽车 | Volkswagen (VOW3.DE)
  - 福特汽车 | Ford (F)
  - 通用汽车 | General Motors (GM)
  - 赣锋锂业 | Ganfeng Lithium
  - 格林美 | GEM
  - 洛阳钼业 | CMOC

# Entity Mapping Guidelines (印尼镍矿工业园专项规则)
- 针对印尼 Weda Bay / IWIP 工业园的劳工和环境问题：由于该园区（IWIP）是由青山控股（Tsingshan Holding）与华友钴业（Huayou Cobalt）等共同投资和运营的核心上游镍不锈钢巨头基地，你在提取事件时，应优先将 entity 映射为「青山控股」或「华友钴业」（若原文明确提到华友的关联，则用华友；否则用青山控股作为运营实体代表）。

# Execution Logic (严格执行)
1. 降噪判定 (is_valid_risk)：
   - 本次回填任务中，必须设定 is_valid_risk 为 true。
2. 严格分类 (risk_category)：
   - "早期合规预警"：政府环保调查、劳工合规审查等（限官方调查性质）。
   - "供应链断裂预警"：供给中断（停产、断供、破产）。
   - "政策与市场准入"：进出口禁令、关税惩罚、实体清单、强迫劳动货物扣留。
   - "合规与运营危机"：劳工罢工/抗意、重大安全事故（爆炸/矿难）、产品召回、车辆起火、严重环保罚单。
   - "机构与声誉预警"：NGO指控、人权机构质询、评级下调等声誉事件。
3. 材料冲击判定 (is_direct_material_impact) 及其 insight 规则：
   - 如果与动力电池、镍/钴/锂矿端停产、安全事故、工业园劳动条件/NGO指控有关，判定为 true。
   - 若判定为 false，executive_insight 必须写为: "该事件属于车企终端运营/技术故障，当前链条未传导至上游材料端。"

# Executive Insight 生成规则 (华友视角)
1. 身份锚定：华友前驱体与正极材料上游供应商，客户包括特斯拉、宝马、奔驰、大众等。
2. 结构铁律：客观事实 + 华友钴业视角传导分析。严禁出现任何“建议”、“需要”等措辞。
3. 字数红线：50-80 汉字。

# Output Format
你必须输出且仅输出一个 JSON 对象：
{
  "events": [
    {
      "entity": "精确匹配上述企业全称之一，例如：青山控股",
      "core_event_title_en": "标准英文核心事件摘要 (5-8个词)",
      "display_title_zh": "高质量汉化且精炼的中文新闻标题",
      "original_language": "原文章语种，如 '俄语'、'英语'、'印尼语'",
      "executive_insight": "客观事实 + 传导分析，50-80字",
      "date": "文章实际发表日期 YYYY-MM-DD，如果未知，使用用户传入的 default_date",
      "sources": [{"name": "媒体名", "url": "原文链接"}],
      "source_urls": ["原文链接"],
      "risk_category": "上述分类之一",
      "is_valid_risk": true,
      "is_direct_material_impact": true
    }
  ]
}
"""

def main():
    token = os.environ.get("NOTION_TOKEN", "")
    database_id = os.environ.get("NOTION_DATABASE_ID", "")
    
    if not token or not database_id:
        logger.error("Error: NOTION_TOKEN or NOTION_DATABASE_ID missing from environment / .env")
        return
        
    logger.info("Initializing LLM provider...")
    llm = create_provider()
    notion = NotionClient(auth=token)
    
    success_count = 0
    
    for key, config in URL_MAPPING.items():
        file_path = Path(config["file"])
        if not file_path.exists():
            logger.warning(f"File not found: {file_path}, skipping {key}")
            continue
            
        logger.info(f"Processing {key} (File: {file_path})")
        raw_content = file_path.read_text(encoding="utf-8")
        
        user_message = f"""
URL: {config["url"]}
Default Date: {config["default_date"]}

--- Raw Content ---
{raw_content[:20000]}
"""
        
        logger.info(f"Calling LLM for extraction of {key}...")
        try:
            result = llm.complete(
                SYSTEM_PROMPT, 
                user_message, 
                temperature=0.1, 
                max_tokens=2048,
                response_format={"type": "json_object"}
            )
            
            if not result:
                logger.warning(f"Failed to get response from LLM for {key}")
                continue
                
            raw_output, usage = result
            logger.info(f"LLM Response received ({usage.summary()})")
            
            # 提取 JSON 对象
            import re
            match = re.search(r"\{.*\}", raw_output, re.DOTALL)
            if not match:
                logger.warning(f"No JSON object in LLM response for {key}")
                continue
                
            parsed = safe_json_parse(match.group(0))
            if not parsed or "events" not in parsed or not isinstance(parsed["events"], list):
                logger.warning(f"Invalid JSON format parsed for {key}")
                continue
                
            events = parsed["events"]
            if not events:
                logger.info(f"No events returned by LLM for {key}")
                continue
                
            # 打印并写入 Notion
            for event in events:
                # 注入必要的元数据
                event["mode"] = "weekly" # 以每周/宏观政策模式写入
                event["push_date"] = datetime.now().strftime("%Y-%m-%d")
                
                # 确保 sources 补全 URL
                if "sources" in event and isinstance(event["sources"], list):
                    for src in event["sources"]:
                        if not src.get("url"):
                            src["url"] = config["url"]
                else:
                    event["sources"] = [{"name": "Web Source", "url": config["url"]}]
                    
                if "source_urls" not in event:
                    event["source_urls"] = [config["url"]]
                    
                if not event.get("date"):
                    event["date"] = config["default_date"]
                
                logger.info(f"Extracted Event Details:")
                logger.info(f"  Entity: {event.get('entity')}")
                logger.info(f"  Title ZH: {event.get('display_title_zh')}")
                logger.info(f"  Date: {event.get('date')}")
                logger.info(f"  Insight: {event.get('executive_insight')}")
                logger.info(f"  Direct Material Impact: {event.get('is_direct_material_impact')}")
                
                logger.info("Upserting to Notion...")
                action, page_id = upsert_notion_page(event, notion, database_id, dry_run=False)
                if action in ("created", "updated"):
                    success_count += 1
                    logger.info(f"Successfully upserted {key} to Notion (action: {action}, page_id: {page_id})")
                    
        except Exception as e:
            logger.exception(f"Error processing {key}: {e}")
            
    logger.info(f"Backfill completed. Total processed and written events: {success_count}")

if __name__ == "__main__":
    main()

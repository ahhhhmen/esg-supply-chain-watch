"""
esg_agent.deduplication — 去重与合并逻辑
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations
import json
import logging
import re
from typing import Optional

logger = logging.getLogger("esg_agent")


class JaccardMerger:
    """同公司同质化事件语义合并（Jaccard 词级相似度）"""

    _MERGE_SIMILARITY_THRESHOLD = 0.45
    _WORD_PATTERN = re.compile(r"\w+", re.UNICODE)

    @classmethod
    def merge(cls, events: list[dict]) -> list[dict]:
        if not events:
            return []

        entity_groups: dict[str, list[dict]] = {}
        for e in events:
            ent = str(e.get("entity", "")).strip().lower()
            entity_groups.setdefault(ent, []).append(e)

        merged: list[dict] = []
        for ent, group in entity_groups.items():
            if len(group) <= 1:
                merged.extend(group)
                continue

            used = [False] * len(group)
            for i in range(len(group)):
                if used[i]:
                    continue
                base = group[i]
                base_title = str(base.get("core_event_title_en", base.get("core_event_title", ""))).strip().lower()
                base_tokens = set(cls._WORD_PATTERN.findall(base_title))

                all_sources: list[dict] = []
                seen_urls: set[str] = set()
                for s in base.get("sources", []) if isinstance(base.get("sources"), list) else []:
                    if isinstance(s, dict):
                        s_url = str(s.get("url", "")).strip()
                        if s_url and s_url not in seen_urls:
                            seen_urls.add(s_url)
                            all_sources.append(s)
                    elif isinstance(s, str):
                        all_sources.append({"name": s, "url": ""})

                all_dates = [str(base.get("date", ""))[:10]]

                for j in range(i + 1, len(group)):
                    if used[j]:
                        continue
                    other = group[j]
                    other_title = str(other.get("core_event_title_en", other.get("core_event_title", ""))).strip().lower()
                    other_tokens = set(cls._WORD_PATTERN.findall(other_title))

                    if not base_tokens or not other_tokens:
                        continue

                    overlap = len(base_tokens & other_tokens)
                    union = len(base_tokens | other_tokens)
                    similarity = overlap / union if union > 0 else 0

                    if similarity >= cls._MERGE_SIMILARITY_THRESHOLD:
                        used[j] = True
                        for s in other.get("sources", []) if isinstance(other.get("sources"), list) else []:
                            if isinstance(s, dict):
                                s_url = str(s.get("url", "")).strip()
                                if s_url and s_url not in seen_urls:
                                    seen_urls.add(s_url)
                                    all_sources.append(s)
                            elif isinstance(s, str):
                                all_sources.append({"name": s, "url": ""})
                        all_dates.append(str(other.get("date", ""))[:10])
                        logger.info(f"[merge] {ent}: {base_title[:50]}... <- {other_title[:50]}... (sim={similarity:.2f})")

                display_zh = str(base.get("display_title_zh") or base.get("core_event_title_en", "")).strip()
                merged_event = {
                    "entity": base.get("entity", ""),
                    "core_event_title_en": base.get("core_event_title_en", base.get("core_event_title", "")),
                    "display_title_zh": display_zh,
                    "original_language": base.get("original_language", ""),
                    "executive_insight": base.get("executive_insight", ""),
                    "date": max(d for d in all_dates if d) if all_dates else base.get("date", ""),
                    "sources": all_sources,
                    "risk_category": base.get("risk_category", ""),
                    "is_valid_risk": base.get("is_valid_risk", True),
                    "is_direct_material_impact": base.get("is_direct_material_impact", True),
                }
                merged.append(merged_event)

        logger.info(f"Semantic merge: {len(events)} raw -> {len(merged)} (threshold={cls._MERGE_SIMILARITY_THRESHOLD})")
        return merged


class LLMGlobalConvergence:
    """终极 LLM 全局聚合层 — 解决跨批次 Semantic Drift"""

    DEEPSEEK_BASE_URL = "https://api.deepseek.com"
    DEEPSEEK_MODEL = "deepseek-chat"

    @classmethod
    def merge(cls, valid_events: list[dict]) -> list[dict]:
        if not valid_events or len(valid_events) <= 1:
            return valid_events

        import os
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            logger.warning("[LLM convergence] DEEPSEEK_API_KEY not set, skip")
            return valid_events

        from openai import OpenAI
        events_json = json.dumps(valid_events, ensure_ascii=False, indent=2)
        system_msg = """You are an event deduplication engine. Merge events describing the same core incident.
Rules:
1. Extract the most accurate display_title_zh.
2. Merge all media names and URLs into sources, preserving original_language.
3. Keep the latest date and the most complete executive_insight.
4. Output must be the same array structure, no extra fields.
5. If no duplicates, return the original array.
Output only valid JSON array, no markdown or explanations."""

        try:
            client = OpenAI(api_key=api_key, base_url=cls.DEEPSEEK_BASE_URL)
            response = client.chat.completions.create(
                model=cls.DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": f"Events JSON array:\n{events_json}"},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=4096,
            )
            raw = response.choices[0].message.content or ""
            match = re.search(r"\[.*\]", raw, re.DOTALL)
            if not match:
                return valid_events
            merged = json.loads(match.group(0))
            if isinstance(merged, list) and len(merged) > 0:
                logger.info(f"[LLM convergence] {len(valid_events)} -> {len(merged)}")
                return merged
        except Exception as exc:
            logger.warning(f"[LLM convergence] failed: {exc}")
        return valid_events

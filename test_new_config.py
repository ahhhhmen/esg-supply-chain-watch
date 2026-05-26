#!/usr/bin/env python3
"""测试新版 config.yaml 解析逻辑"""
import yaml
from pathlib import Path

raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))

print("=== Companies ===")
for c in raw["companies"]:
    print(f"  id={c['id']} short_zh={c['short_name_zh']} short_en={c['short_name_en']} full_zh={c['full_name_zh']} full_en={c['full_name_en']}")

print("\n=== Topics ===")
for t in raw["topics"]:
    print(f"  id={t['id']} zh={t['zh']} en={t['en']} id={t['id']} fr={t['fr']}")

print("\n=== Query Matrix (first 5) ===")
languages = raw["languages"]
count = 0
for company in raw["companies"]:
    short_zh = company.get("short_name_zh", company["id"])
    short_en = company.get("short_name_en", company["id"])
    full_zh = company.get("full_name_zh", short_zh)
    full_en = company.get("full_name_en", short_en)

    for topic in raw["topics"]:
        for lang in languages:
            comp_name = short_zh if lang == "zh" else short_en
            topic_label = topic.get(lang) or topic.get("en", topic["id"])
            query = f"{comp_name} {topic_label}"
            count += 1
            if count <= 5:
                print(f"  [{lang}] company={comp_name} topic={topic_label}")
                print(f"       query={query}")
                print(f"       display={full_zh} | {full_en}")

print(f"\n  ... total {count} queries")
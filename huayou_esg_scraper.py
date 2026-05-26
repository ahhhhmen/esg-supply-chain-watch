#!/usr/bin/env python3
"""
华友钴业 ESG 新闻抓取工具 v6
数据源: Bing News RSS (主力) + Google News RSS + DuckDuckGo (备用)
策略: 多关键词批量查询，Bing RSS 为主力稳定通道
产出: huayou_esg_news.csv + huayou_esg_report.md
"""

import csv
import re
import time
import random
from datetime import datetime
from urllib.parse import quote

import pandas as pd
import requests

# ── 关键词池 ────────────────────────────────────────────────
QUERIES_ZH = [
    "华友钴业 ESG",
    "华友钴业 可持续发展",
    "华友钴业 供应链 尽责",
    "华友钴业 碳中和 绿色",
    "华友钴业 镍钴锂 矿产 尽责",
    "华友钴业 电池 回收",
    "镍钴锂 ESG 供应链 尽责 管理",
    "电池材料 供应链 可持续发展",
    "动力电池 回收 可持续",
]

QUERIES_EN = [
    "Huayou Cobalt ESG sustainability",
    "Huayou Cobalt supply chain due diligence",
    "cobalt mining ESG battery supply chain",
]


# ── 1. Bing News RSS（主力数据源）─────────────────────────
def search_bing_news_rss(query: str, max_results: int = 10) -> list[dict]:
    """Bing News RSS — 在中国大陆可稳定访问，无需 API Key。"""
    results = []
    url = f"https://www.bing.com/news/search?q={quote(query)}&format=rss&first=1"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        items = re.findall(r"<item>(.*?)</item>", resp.text, re.DOTALL)
        for item in items[:max_results]:
            title_m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item)
            link_m = re.search(r"<link>(.*?)</link>", item)
            pubdate_m = re.search(r"<pubDate>(.*?)</pubDate>", item)
            source_m = re.search(r"<News:Source>(.*?)</News:Source>", item)

            title = title_m.group(1).strip() if title_m else ""
            link = link_m.group(1).strip() if link_m else ""
            pubdate = pubdate_m.group(1).strip() if pubdate_m else ""
            source = source_m.group(1).strip() if source_m else "Bing News"

            if title and link:
                results.append({
                    "title": title, "date": pubdate, "source": source,
                    "url": link, "query": query, "origin": "BingNewsRSS",
                })
    except requests.exceptions.Timeout:
        pass
    except Exception:
        pass
    return results


# ── 2. Google News RSS ─────────────────────────────────────
def search_google_news_rss(query: str, max_results: int = 10) -> list[dict]:
    """Google News RSS"""
    results = []
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            return results
        items = re.findall(r"<item>(.*?)</item>", resp.text, re.DOTALL)
        for item in items[:max_results]:
            title_m = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>", item)
            link_m = re.search(r"<link>(.*?)</link>", item)
            pubdate_m = re.search(r"<pubDate>(.*?)</pubDate>", item)
            source_m = re.search(r'source="(.*?)"', item)

            title = title_m.group(1).strip() if title_m else ""
            link = link_m.group(1).strip() if link_m else ""
            pubdate = pubdate_m.group(1).strip() if pubdate_m else ""
            source = source_m.group(1).strip() if source_m else "Google News"

            if title and link:
                results.append({
                    "title": title, "date": pubdate, "source": source,
                    "url": link, "query": query, "origin": "GoogleNewsRSS",
                })
    except Exception:
        pass
    return results


# ── 3. DuckDuckGo ──────────────────────────────────────────
def search_ddg_news(query: str, max_results: int = 10) -> list[dict]:
    """DuckDuckGo News（可能被限速，作为备用）"""
    try:
        from duckduckgo_search import DDGS

        results = []
        with DDGS() as ddgs:
            news_gen = ddgs.news(
                keywords=query, region="cn-zh", safesearch="off",
                timelimit="m", max_results=max_results,
            )
            for item in news_gen:
                results.append({
                    "title": item.get("title", ""),
                    "date": item.get("date", ""),
                    "source": item.get("source", ""),
                    "url": item.get("url", ""),
                    "query": query,
                    "origin": "DuckDuckGo",
                })
        if results:
            print(f"    ✓ DDG: +{len(results)}")
        return results
    except Exception:
        return []


# ── 4. Markdown 报告生成 ──────────────────────────────────
def generate_markdown_report(df: pd.DataFrame, output_path: str):
    """按 source 分组生成排版美观的 Markdown 报告"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    lines.append("# 华友钴业 ESG 新闻简报")
    lines.append("")
    lines.append(f"> 自动生成时间：{now}  |  数据来源：Bing News RSS / Google News RSS / DuckDuckGo")
    lines.append(f"> 新闻总数：{len(df)} 条（按标题去重）  |  覆盖来源：{df['source'].nunique()} 家媒体")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 📌 目录")
    lines.append("")

    # 按 source 分组，组内按日期降序
    grouped = df.groupby("source", sort=False)
    source_order = df["source"].value_counts().index.tolist()

    for source in source_order:
        count = len(grouped.get_group(source))
        anchor = source.replace(" ", "-")
        lines.append(f"- [{source}](#{anchor})（{count} 条）")

    lines.append("")
    lines.append("---")
    lines.append("")

    for source in source_order:
        group = grouped.get_group(source).sort_values("_date_parsed", ascending=False)
        count = len(group)
        lines.append(f"## {source}")
        lines.append("")
        lines.append(f"> 共 {count} 篇相关报道")
        lines.append("")
        lines.append("| # | 发布日期 | 标题 |")
        lines.append("|---|---|---|")
        for i, (_, row) in enumerate(group.iterrows(), 1):
            date_str = str(row["date"])
            try:
                date_str = pd.to_datetime(row["date"], utc=True).strftime("%Y-%m-%d")
            except Exception:
                pass
            title = row["title"].replace("|", "\\|")
            url = row["url"]
            lines.append(f"| {i} | {date_str} | [{title}]({url}) |")
        lines.append("")
        lines.append("---")
        lines.append("")

    # 页脚
    lines.append("> ⚠ 本报告由自动化脚本生成，数据可能存在延迟或遗漏，仅供决策参考。")
    lines.append("")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"✅ Markdown 报告已保存到 {output_path}")


# ── 主流程 ──────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  华友钴业 ESG 新闻抓取工具 v6")
    print(f"  运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    all_results = []
    seen_urls = set()
    all_queries = QUERIES_ZH + QUERIES_EN

    for idx, query in enumerate(all_queries, 1):
        print(f"\n[{idx}/{len(all_queries)}] {query}")

        # Bing RSS 主力
        bing = search_bing_news_rss(query)
        if bing:
            print(f"    ✓ Bing RSS: +{len(bing)}")
        for r in bing:
            if r["url"] not in seen_urls:
                seen_urls.add(r["url"])
                all_results.append(r)

        # Google RSS
        google = search_google_news_rss(query)
        if google:
            print(f"    ✓ Google RSS: +{len(google)}")
        for r in google:
            if r["url"] not in seen_urls:
                seen_urls.add(r["url"])
                all_results.append(r)

        # DDG 备用
        ddg = search_ddg_news(query, max_results=8)
        for r in ddg:
            if r["url"] not in seen_urls:
                seen_urls.add(r["url"])
                all_results.append(r)

        wait = random.uniform(2.0, 3.0)
        time.sleep(wait)

    # ── 数据处理 ──────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"📊 原始抓取: {len(all_results)} 条 (URL 已去重)")

    if not all_results:
        print("⚠ 无结果。生成空文件。")
        pd.DataFrame(columns=["title", "date", "source", "url", "query", "origin"]).to_csv(
            "huayou_esg_news.csv", index=False, encoding="utf-8-sig", quoting=csv.QUOTE_ALL,
        )
        return

    df = pd.DataFrame(all_results)

    # 解析日期列
    df["_date_parsed"] = pd.to_datetime(df["date"], errors="coerce", utc=True)

    # ── 关键：按 title 二次去重（Bing RSS 不同 query 可能返回重复结果）──
    before_dedup = len(df)
    df = df.drop_duplicates(subset=["title"], keep="first")
    after_dedup = len(df)
    print(f"🔧 按标题去重: {before_dedup} → {after_dedup} 条（移除 {before_dedup - after_dedup} 条重复）")

    # 日期排序
    df = df.sort_values("_date_parsed", ascending=False, na_position="last")

    # ── 保存 CSV（不含内部 _date_parsed 列）─────────────────
    df_csv = df[["title", "date", "source", "url", "query", "origin"]]
    csv_path = "huayou_esg_news.csv"
    df_csv.to_csv(csv_path, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_ALL)
    print(f"✅ CSV 已保存: {csv_path} ({len(df_csv)} 条)")

    # ── 生成 Markdown 报告 ─────────────────────────────────
    md_path = "huayou_esg_report.md"
    generate_markdown_report(df, md_path)

    # ── 终端摘要 ──────────────────────────────────────────
    print(f"\n📊 数据源分布:")
    for k, v in df["origin"].value_counts().items():
        print(f"  {k}: {v}")

    print(f"\n📊 媒体来源 Top 10:")
    for source, count in df["source"].value_counts().head(10).items():
        print(f"  {source}: {count}")

    print(f"\n📋 最新 5 条:")
    for _, row in df.head(5).iterrows():
        t = row["title"][:70]
        d = str(row["date"])[:16]
        print(f"  [{row['source'][:14]}] {d}  {t}")


if __name__ == "__main__":
    main()
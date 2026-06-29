# ESG Supply Chain Watch — Workspace Custom Rules & Logic Consensus

This document defines the core domain rules and logic constraints for this specific workspace. Future agents must read this file first and strictly follow these rules to maintain logic consistency and prevent regressions.

---

## 1. Project Positioning & Pipeline
- AI 驱动的多语言 ESG 风险情报监控与预警系统，围绕华友钴业及 12 家电池材料供应链企业。
- 6 阶段流水线：多语言采集 → 去重 → 实体校验 → LLM 语义过滤 → 合并/渲染 → 推送/归档。
- 三种运行模式：daily（运营风险）、weekly（宏观政策/地缘合规）、practice（行业最佳实践）。
- 5 大风险类别：供应链中断、政策/市场准入、合规/运营危机、机构/声誉预警、合规早期预警。

## 2. Tech Stack & Conventions
- Python 3.10+，主引擎 `esg_intelligence_agent.py`（~2800 行）。
- DeepSeek API 为主要 LLM，可选 Tavily 相关性评分。
- 依赖 `radar-infra` 共享库（LLM Provider、CachedLLMClient、retry、MetricsStore）。
- OpenAI SDK（openai>=1.65.4）用于 DeepSeek 兼容调用。
- 中文为主的代码注释和文档字符串，英文变量名。
- `from __future__ import annotations` 统一使用。
- esg_agent/ 包（8 模块）：config、fetchers、filters、deduplication、reporters、scorer、validators、llm_client、canonical、pdf_writer。

## 3. Domain Logic & Data Filters
- **4 语言 4 地理轨**：zh-CN、en-US、id-ID、fr-FR，各使用本地化 Google News 参数。
- **三层采集**：静态 RSS + 动态查询矩阵 + AI 发现查询（Phase 0.5）。
- **实体过滤器**：正则匹配 12 家目标公司名（支持 CJK），垃圾/赌博关键词黑名单。
- **去重**：Jaccard 词级相似度（阈值 0.45）+ LLM 跨批次语义收敛。
- **规范化事件键**：`canonical_event_key()` 使用实体别名、信号组、日期分桶生成稳定键值。
- **Fail-closed 设计**：LLM JSON 提取失败的批次整体丢弃，不降级为原始数据。
- **双重重要性分级**：直接影响（红色）vs 战略观察（黄色），观察级不推钉钉主报告。
- **Google News 密文解码**：在 `SourcingEngine` 及 `resolve_news_url` 中置入本地 Base64-Protobuf 解码器，自动解析 `news.google.com/rss/articles/` 中的原始真实链接，彻底避免 400 Bad Request 错误及网络请求延迟。

## 4. Configuration & Sources
- `config.yaml`（222 行）：12 家公司、4 地理轨、多语言风险主题关键词、排除模式。
- `esg_sources.yaml`：5 条静态雷达查询（BYD 欧洲、华友非洲、BHRRC、紫金全球、美国海关 WRO）。
- Google News RSS 为主采集渠道，BHRRC 和 EFRAG 为专项轨。
- `days_limit: 7`，每公司上限 20 条。
- ThreadPoolExecutor：RSS 20 线程、内容提取 10 线程、LLM BATCH_SIZE=15。

## 5. Output & Scheduling
- **Markdown 报告**：`esg_global_report.md`（日报/周报）、`esg_practice_report.md`（最佳实践）。
- **PDF 归档**：`reports/YYYY-MM-DD_{mode}.pdf`。
- **Notion 双库**：ESG Risk + ESG Practice，SHA-256 确定性 External ID 幂等 upsert。
- **钉钉推送**：采用优化视觉层级的 Markdown 块状可视化排版，在标题中嵌入超链接直接跳转至洗白后的原文，其余判定依据、高管洞察及来源统计均收纳在引用块（`>`）中，极大降低认知负荷。
- **Metrics**：`metrics.jsonl` 追加式运行指标（时间戳、模式、token 用量、成本、事件数、风险分布）。
- GitHub Actions 三条定时工作流：daily（周一至四、六日）、weekly（周四）、practice（周三），均在 UTC 22:00。
- 成本追踪：$0.14/M input tokens、$0.28/M output tokens。

## 6. Local-First & Privacy Constraints
- `.env` 含真实 API 密钥，已在 `.gitignore` 中排除。
- 所有输出（reports/、pdfs/、*.pdf）已 gitignore。
- Notion 幂等写入设计文档：`docs/notion_idempotent.md`。

---

<!-- This file is the single source of truth. .cursorrules, .windsurfrules, .github/copilot-instructions.md, and CLAUDE.md all symlink here. -->

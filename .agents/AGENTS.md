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
- **5 语言 5 地理轨**：zh-CN、en-US、id-ID、fr-FR、ru，各使用本地化 Google News 参数。此外，印尼语 (id-ID) 日常劳工轨增补了关于长工时、猝死及自杀等职业安全与健康的负面词库。
- **三层采集**：静态 RSS + 动态查询矩阵 + AI 发现查询（Phase 0.5）。
- **实体过滤器**：正则匹配 12 家目标公司名（支持 CJK），垃圾/赌博关键词黑名单。
- **去重**：Jaccard 词级相似度（阈值 0.45）+ LLM 跨批次语义收敛。
- **规范化事件键与稳定 External ID**：`canonical_event_key()` 使用实体别名、信号组、日期分桶生成稳定键值。**严禁**将非确定性的大模型解读（`insight`/`executive_insight`）混入 Key 和 External ID 计算中。**优先使用** `core_event_title_en`（标准英文短摘要）联合 `entity` 生成 Fallback Token Key，确保多次跑批或大模型局部变动时算出的 External ID 强确定性一致，防止 Notion 产生重复记录。
- **Fail-closed 设计**：LLM JSON 提取失败的批次整体丢弃，不降级为原始数据。
- **双重重要性分级**：直接影响（红色）vs 战略观察（黄色），观察级不推钉钉主报告。
- **Google News 密文解码与 URL 防护**：在 `SourcingEngine` 及 `resolve_news_url` 中，优先使用 `googlenewsdecoder` 作为首选解码机制，并配置 `_is_valid_news_url` 过滤规则。禁止将流量统计、社交分享、广告追踪及 Google 静态接口等非新闻域名（如 `google-analytics.com`、`googletagmanager.com`、`fonts.googleapis.com`）及 `.js`/`.css` 等静态资产提取为新闻直链。
- **人权与高防网站抗爬回退机制**：针对 BHRRC (`business-humanrights.org`) 和 Evidencity (`evidencity.com`) 等设有强力 Cloudflare 盾的站点，如果常规 `extract_article_body` 抓取返回 403 / 失败，系统自动回退请求 `https://r.jina.ai/<URL>` 获取干净的正文 Markdown，并剥离 Jina 注入的 metadata 头部，只截取 `max_length` 长度，保证大模型能够获得稳定的正文摘要。
- **持久化去重记忆库 (Persistent Cache)**：在 `logs/processed_urls.json` 中记录过去 14 天内已处理的文章链接 (URL)，SourcingEngine 在初始化时自动加载并清理过期记录，在抓取解析的 loop 中第一时间拦截已存在链接，防止重复数据进入大模型。新 URL 会在汇总后增量写入文件。
- **LLM 时间校验铁律与历史隐患深度复盘例外 (Prompt Reinforcement)**：大模型 System Prompt 强制注入系统时间 `今天是 {current_date}。` 对于普通新闻，早于限制时间的事件直接判定为失效情报。例外规则：若为最近 7 天内刚被主流媒体/权威 NGO 首次/最新【深度曝光、复盘追责】的过往重大隐患，判定为 `is_valid_risk = true` 归入“机构与声誉预警”或“合规与运营危机”，并注明【历史隐患深度复盘/追责】。同时，因辅料暴涨、辅料断供或设备高负荷检修导致的 30% 以上重大物理停产/产能扣减，必须判定为 `is_valid_risk = true` 归入“供应链断裂预警”。
- **子实体与别名正则界定 (Subsidiary Aliases & ASCII-Boundary Protection)**：监控企业可在 `config.yaml` 中配置 `aliases` 关联子公司与项目（如华越 PT Huayue、华飞 PT Huafei、IWIP、IMIP 等）。在 RSS 查询及 `EntityFilter` 比对中自动包含别名。`EntityFilter._build_pattern` 必须使用 `(?<![a-zA-Z0-9])` 与 `(?![a-zA-Z0-9])` 替代 `\b`，解决 CJK 字符与英文紧挨（如 `"园区PT Huayue"`）导致 `\b` 界定失灵的问题。
- **主机厂非核心事故与实体错误过滤 (Chemical & Scope Filters)**：对于发生在非监控车企自身主体（如航空航天或非核心零部件供应商 GKN 等）的化学品/环保安全事故，即使标题中包含车企关键词或标签，LLM 必须判定 `is_valid_risk = false` 降噪拦截；同时，主机厂常规厂区发生的普通化学品泄漏、常规环保/安全事故或非电池核心零部件污染，对上游电池材料没有直接的供应链穿透冲击，必须判定 `is_direct_material_impact = false`（作为黄色“战略观察”处理，不推钉钉主报告），且其 `executive_insight` 强制套用硬性模板：`"该事件属于车企终端运营/技术故障，当前链条未传导至上游材料端。"`
- **整车厂劳资博弈与评级一致性 (OEM Labor & Materiality Guardrails)**：整车厂（OEM）端常规劳资谈判、工会目标指定、无偿加班/降薪争议、工时与整车产量微调（未确认导致电池工厂停产或电池材料订单确认削减前），必须判定为 `is_direct_material_impact = false`（黄色“战略观察”）。Python 侧 `_apply_materiality_guardrails` 强制使用纯新闻事实（`_factual_event_text`）校验提升规则，严禁 LLM 在洞察中推算出的间接假设（如“可能影响电动车减产”）误将事件升级为红色冲击。若洞察中出现“间接”、“未确认”、“暂未传导”，强制判为 `is_direct_material_impact = false`。
- **统一语义合并与跨列表去重 (Unified Cross-List Deduplication)**：`_generate_v10_report_and_filter` 必须在划分 `valid_events` (🔴) 与 `watch_events` (🟡) 之前，对所有有效风险事件执行统一语义合并，并在划分后执行 `valid_keys` 交叉碰撞，合并同键来源并剔除观察清单重复项，彻底隔离“一事两报”与评级冲突。


## 4. Configuration & Sources
- `config.yaml`：12 家公司、5 地理轨、多语言风险主题与良好实践关键词、排除模式。
- `esg_sources.yaml`：24 条静态雷达查询（包含 BYD 欧洲、华友非洲、BHRRC、紫金全球、美国海关 WRO、印尼 HPAL 湿法尾矿、CRI 人权预警、IT之家 产业雷达、汽车之家 行业雷达、ACEA 欧洲车协、AM-Online、德国之声 DW、路透社 Autos、Automotive News、Electrive 欧洲 EV、InsideEVs 技术、Electrek 北美、CleanTechnica 政策、The EV Report、盖世汽车 Gasgoo、Paul Tan 东南亚、Benchmark Mineral 电池矿产、Fastmarkets 大宗）。
- Google News RSS 为主采集渠道，BHRRC 和 EFRAG 为专项轨。
- `days_limit: 7`，每公司上限 20 条。
- ThreadPoolExecutor：RSS 20 线程、内容提取 10 线程、LLM BATCH_SIZE=15。

## 5. Output & Scheduling
- **Markdown 报告**：`esg_global_report.md`（日报/周报）、`esg_practice_report.md`（最佳实践）。
- **PDF 归档**：`reports/YYYY-MM-DD_{mode}.pdf`。
- **Notion 双库**：ESG Risk + ESG Practice，SHA-256 确定性 External ID 幂等 upsert。
- **钉钉推送**：采用优化视觉层级的 Markdown 块状可视化排版，在标题中嵌入超链接直接跳转至洗白后的原文，其余判定依据、高管洞察及来源统计均收纳在引用块（`>`）中，极大降低认知负荷。
- **周报排版与归档顺序**：周报（及推送消息）目录必须列出「📡 战略观察清单」及「🔍 监控矩阵盲区分析」。在排版上，盲区分析必须放置在战略观察清单之后、页脚之前。主引擎执行时，必须先生成盲区分析并插入报告，再追加 Token 消耗脚标，最后执行报告的 MD 归档与 PDF 转换，以确保归档文件完整。
- **盲区分析 LLM 约束**：盲区分析 System Prompt 内含严格负向约束，绝对禁止输出任何对话性、寒暄性前言（例如“好的，作为ESG监控系统架构师...”），必须直接输出以 markdown 列表或标题开头的分析内容。
- **Metrics**：`metrics.jsonl` 追加式运行指标（时间戳、模式、token 用量、成本、事件数、风险分布）。
- GitHub Actions 三条定时工作流：daily（周一至四、六日）、weekly（周四）、practice（周三），均在 UTC 22:00。
- 成本追踪：$0.14/M input tokens、$0.28/M output tokens。
- **记忆库自动推送**：GitHub Actions 工作流（每日/地缘周报/实践周报）跑批完成后，自动将更新后的 `logs/processed_urls.json` 进行 git add, commit 并 push 推送回代码仓库，保证次日虚拟机加载最新记忆。


## 6. Local-First & Privacy Constraints
- `.env` 含真实 API 密钥，已在 `.gitignore` 中排除。
- 所有输出（reports/、pdfs/、*.pdf）已 gitignore。
- Notion 幂等写入设计文档：`docs/notion_idempotent.md`。

## 7. Long-Term Agent Memory
- 对话中的关键共识不会自动写入仓库文件；需要长期保留的规则、业务判断、Prompt 约束和工作流约定，必须由用户明确要求后沉淀到本文件。
- 本文件是长期记忆的单一事实源；`AGENTS.md`、`.cursorrules`、`.windsurfrules`、`.github/copilot-instructions.md` 均应通过软链接或同步内容指向本文件，避免多处规则漂移。
- 新增长期记忆时，只记录稳定、可复用、会影响未来实现或判断的规则；不要记录一次性任务过程、临时日志、敏感密钥、个人隐私或尚未确认的猜测。
- 修改长期记忆时，应优先追加到最相关章节；若规则会改变既有行为，必须明确写出新规则的适用范围，避免覆盖原有业务红线。
- 当前已确认的 Prompt 质量与地缘推送排版规则：
  - **公共法案与地缘政策去重**：`canonical_event_key()` 必须包含欧盟电池法案授权法案质量平衡系统、津巴布韦锂禁令、中国两用物项管制公告等硬规则，并在 `_SIGNAL_GROUPS` 中扩展政策法规与回收比率信号词，确保同一法案跨媒体报道强确定性合并。
  - **推送排版与分级标识禁令**：钉钉 Markdown 推送元信息必须严格收纳于引用块 `>` 中，标题内嵌洗白后的原文 URL `[实体 | 标题](URL)`。`🟡 战略观察` / `🟢 低度监测` 事件绝对禁挂 `🚨` 或 `🔴 重磅预警` 标识，必须单独输出至「📡 战略观察清单」。
  - **Notion 去重与实体归一法则**：`audit_notion_duplicates.py` 与 `clean_notion.py` 用于数据库多维反向去重（精确 External ID、Canonical Event Key 语义、新闻 URL 直链及垃圾页面）。`notion_mapping.py` 中的 `map_entity` 必须自动将印尼项目及子公司别名（如 `"华飞"`、`"华越"`、`"IWIP"`、`"IMIP"`）映射至对应的监控主体公司（如 `"华友钴业"`），禁止将已知监控企业的子公司归入 `"其他"` 实体。

---

<!-- This file is the single source of truth. .cursorrules, .windsurfrules, .github/copilot-instructions.md, and AGENTS.md all symlink here. -->

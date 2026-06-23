# ESG 全球供应链风险情报监控平台 🔍

基于 AI 的多语种 ESG 风险情报自动化监控与推送系统，以**华友钴业**为中心，覆盖 12 家新能源电池材料供应链关键企业。

## 核心能力

- **多语种全球监控**: 中文/英语/印尼语/法语，4 个语种定向 Google News 抓取
- **6 阶段智能流水线**: 供料 → 去重 → 实体校验 → LLM 语义降噪 → 合并渲染 → 推送归档
- **双频动态播报**: `daily`（日常运营风险）/ `weekly`（宏观政策与地缘合规）
- **四重风险标签**: 供应链断裂 / 政策市场准入 / 合规运营危机 / 机构声誉预警 / 早期合规预警
- **华友钴业中心制**: 所有高管洞察从华友钴业的产业位置出发进行传导推演
- **多渠道推送**: 钉钉 Webhook + Notion Database（幂等 upsert）

## 技术架构

```
esg_agent/                    # 核心模块
├── config.py                 # 配置管理与双频路由
├── fetchers.py               # RSS 抓取 + 内容提取
├── filters.py                # 实体校验 + 漏斗限流
├── deduplication.py          # Jaccard 语义去重 + LLM 全局聚合
├── reporters.py              # Markdown 报告 + 钉钉/Notion 推送
├── scorer.py                 # Tavily 第三方相关性评分
├── validators.py             # Pydantic 配置验证
└── pdf_writer.py             # Markdown → PDF 转换

config.yaml                   # 12 企业 + 4 语种 + 6 主题矩阵
esg_sources.yaml              # 静态 RSS 抓取轨道
esg_intelligence_agent.py     # 主入口（v9 流水线）
```

共享基础设施（LLM 缓存/重试/抓取/推送等）由 [radar-infra](https://github.com/ahhhhmen/radar-infra) 提供。

## 快速开始

```bash
# 1. 克隆项目
git clone https://github.com/ahhhhmen/company-research-agent.git
cd company-research-agent

# 2. 创建 .env 文件，至少填入以下变量
cat > .env << 'EOF'
DEEPSEEK_API_KEY=sk-your-key-here
# 可选：OpenAI 作为 fallback
# OPENAI_API_KEY=sk-your-openai-key
# 可选：Notion 数据库写入
# NOTION_TOKEN=secret_xxx
# NOTION_DATABASE_ID=xxx
# NOTION_PRACTICE_DATABASE_ID=xxx
# 可选：钉钉推送
# DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxx
EOF

# 3. 安装依赖（自动拉取 radar-infra）
pip install -r requirements.txt

# 4. 运行每日舆情监控
python esg_intelligence_agent.py --mode daily

# 5. 运行每周宏观政策周报
python esg_intelligence_agent.py --mode weekly --no-push
```

## GitHub Actions 自动化

- **每日监控** (UTC 22:00 周一-周六): `.github/workflows/esg_monitor.yml`
- **每周周报** (UTC 22:00 周四): `.github/workflows/esg_policy_weekly.yml`

## 运行指标

每次运行自动收集并持久化到 `metrics.jsonl`：

```json
{
  "supply": {"static": 0, "dynamic": 156, "ai_discovery": 5},
  "llm": {"batches": 12, "total_tokens": 45000, "cost_usd": 0.008},
  "output": {"valid_events": 3, "material_events": 1, "final_items": 1}
}
```

## 历史项目

本项目源自一个多 Agent 公司研究工具（Tavily + Gemini + GPT-4.1）。该代码已归档到 `deprecated/old-company-research/`。

## License

MIT

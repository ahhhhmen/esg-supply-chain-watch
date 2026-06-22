# AI Context: ESG 全球供应链风险情报监控平台

## 项目定位
为华友钴业打造的多语种 ESG 风险情报自动化监控系统。

## 核心架构
- 主入口: esg_intelligence_agent.py (1765行, v9)
- 模块: esg_agent/ (8个子模块)
- 配置: config.yaml (12企业+4语种+6主题)
- CI/CD: GitHub Actions 双频 (daily/weekly)

## 六阶段流水线
0.5. AI动态搜索词 1. 三层供料 2. 去重 2.5. 实体校验 2.6. 漏斗限流
3. 正文提取 4. LLM降噪 5. 渲染合并 6. [weekly] 盲区分析

## 历史
原公司研究工具已归档到 deprecated/old-company-research/

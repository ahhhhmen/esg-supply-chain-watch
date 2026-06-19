# Notion 幂等写入方案

## 问题背景

之前共存在两处 Notion 写入路径（`backfill_notion.py` 的回填流程 和 `esg_intelligence_agent.py` 的每日推送），它们各自使用内存中的 `dedup_key`（`entity|date|title[:50]`）做去重判断。由于这个 key 是易失的（不持久化），同一事件可能被多次创建，导致 Notion 数据库中出现重复条目。

## 解决方案

引入持久化的 **External ID** 字段实现幂等 upsert。

### 架构

```
notion_upsert.py  ← 共享 upsert 助手（唯一写入入口）
   ↑                    ↑
backfill_notion.py   esg_intelligence_agent.py
```

### 工作流程

1. **生成 External ID**：对每条事件计算 `hash(entity|date|title)` → 12 位十六进制字符串
2. **查询 Notion**：在数据库中搜索 `External ID` 匹配的页面
3. **幂等写入**：
   - 页面不存在 → 创建新页面（写入 External ID）
   - 页面已存在 → 更新已有页面
4. **Dry-run 支持**：`dry_run=True` 时仅打印日志，不执行 API 调用

## 数据库 Schema 迁移

需要在 Notion ESG 数据库中手动添加一个 **External ID** 属性。

### 操作步骤

1. 打开 ESG Intelligence Notion 数据库
2. 点击右上角 `...` → `Properties`
3. 添加新属性：
   - **Name**: `External ID`
   - **Type**: `Text`
4. 保存

> ⚠️ 此属性必须存在，否则 `notion_upsert.py` 在创建新页面时会因未知属性而失败。

### 为已有页面回填 External ID

运行一次性脚本为历史页面计算并写入 External ID：

```bash
# 干跑：仅打印即将写入的 External ID
python scripts/backfill_external_ids.py --dry-run

# 正式写入
python scripts/backfill_external_ids.py
```

> 此脚本需要 NOTION_TOKEN 和 NOTION_DATABASE_ID 环境变量。

## 确定性 ID 格式

```
External ID = SHA-256("大众汽车|2026-06-18|大众汽车宣布在德国裁员1.9万人")[:12]
            = "a3b7c9d1e2f4"
```

- 始终 12 位小写十六进制字符
- 相同输入 → 相同输出（确定性）
- 碰撞概率 < 10⁻⁹（对于数据库规模而言足够）

## 代码变更摘要

| 文件 | 变更 |
|------|------|
| `notion_upsert.py` | **新增** — 共享 upsert 助手模块 |
| `notion_mapping.py` | 新增 `EXTERNAL_ID = "External ID"` 常量 |
| `backfill_notion.py` | 移除 `dedup_key()` 和 `build_notion_properties()`、内联查询逻辑；改用 `upsert_notion_page()` |
| `esg_intelligence_agent.py` | `push_to_notion()` 改用 `upsert_notion_page()`；移除直接 properties 构建 |
| `tests/test_notion_upsert.py` | **新增** — 12 个单元测试（ID 生成、属性构建、创建/更新/干跑） |
| `docs/notion_idempotent.md` | **新增** — 本文档 |

## 测试

```bash
# 运行 upsert 相关测试
pytest tests/test_notion_upsert.py -v

# 运行全部测试
pytest tests/ -v
```

全部 42 个测试应通过（12 新增 + 30 已存在）。

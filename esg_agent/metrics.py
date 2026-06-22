"""
esg_agent.metrics — 运行监控指标与统计
═══════════════════════════════════════════════════════════════════════════════
每次 run() 结束后收集关键指标，输出到日志并可选持久化到 JSON 文件。
"""

from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("esg_agent")


@dataclass
class RunMetrics:
    """单次运行的完整指标快照"""

    # 时间
    timestamp: str = ""
    mode: str = "daily"
    elapsed_seconds: float = 0.0

    # 供料阶段
    static_items: int = 0       # esg_sources.yaml 静态轨道
    dynamic_items: int = 0      # config.yaml query_tasks 动态矩阵
    ai_discovery_items: int = 0 # AI 动态发现
    total_raw_items: int = 0    # 合并后总数

    # 过滤阶段
    after_dedup: int = 0        # URL/标题去重后
    after_entity_filter: int = 0 # 实体校验后
    after_throttle: int = 0     # 企业漏斗限流后

    # LLM 阶段
    llm_batches: int = 0        # 批次数
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_total_tokens: int = 0
    llm_cost_usd: float = 0.0

    # 产出阶段
    total_events: int = 0       # LLM 原始 events
    valid_events: int = 0       # is_valid_risk=true
    material_events: int = 0    # is_direct_material_impact=true
    final_report_items: int = 0 # 最终写入报告的条目

    # 按风险类别分布
    risk_distribution: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "mode": self.mode,
            "elapsed_s": round(self.elapsed_seconds, 1),
            "supply": {
                "static": self.static_items,
                "dynamic": self.dynamic_items,
                "ai_discovery": self.ai_discovery_items,
                "total": self.total_raw_items,
            },
            "filter": {
                "after_dedup": self.after_dedup,
                "after_entity": self.after_entity_filter,
                "after_throttle": self.after_throttle,
            },
            "llm": {
                "batches": self.llm_batches,
                "input_tokens": self.llm_input_tokens,
                "output_tokens": self.llm_output_tokens,
                "total_tokens": self.llm_total_tokens,
                "cost_usd": round(self.llm_cost_usd, 6),
            },
            "output": {
                "total_events": self.total_events,
                "valid_events": self.valid_events,
                "material_events": self.material_events,
                "final_items": self.final_report_items,
            },
            "risk_distribution": self.risk_distribution,
        }

    def log_summary(self) -> None:
        """输出结构化指标摘要到日志"""
        logger.info("═══ Run Metrics Summary ═══")
        logger.info(
            f"Supply: {self.static_items} static + {self.dynamic_items} dynamic "
            f"+ {self.ai_discovery_items} AI = {self.total_raw_items} raw"
        )
        logger.info(
            f"Filter: {self.total_raw_items} → {self.after_dedup} dedup "
            f"→ {self.after_entity_filter} entity → {self.after_throttle} throttle"
        )
        logger.info(
            f"LLM: {self.llm_batches} batches, "
            f"{self.llm_total_tokens:,} tokens, ${self.llm_cost_usd:.4f}"
        )
        logger.info(
            f"Output: {self.total_events} events → {self.valid_events} valid "
            f"→ {self.material_events} material → {self.final_report_items} report items"
        )
        if self.risk_distribution:
            dist = ", ".join(f"{k}:{v}" for k, v in self.risk_distribution.items())
            logger.info(f"Risk distribution: {dist}")
        logger.info(f"Elapsed: {self.elapsed_seconds:.1f}s")


class MetricsStore:
    """指标持久化存储（JSON 文件，追加模式）"""

    def __init__(self, file_path: str = "metrics.jsonl"):
        self._path = Path(file_path)

    def append(self, metrics: RunMetrics) -> None:
        """追加一条运行指标记录（JSONL 格式，每行一条记录）"""
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(metrics.to_dict(), ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"Failed to persist metrics: {e}")

    def load_all(self) -> list[dict]:
        """加载所有历史指标记录"""
        if not self._path.exists():
            return []
        records = []
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return records

    def get_stats_summary(self, last_n: int = 30) -> dict:
        """计算最近 N 次运行的趋势统计"""
        records = self.load_all()[-last_n:]
        if not records:
            return {}

        costs = [r.get("llm", {}).get("cost_usd", 0) for r in records]
        items = [r.get("output", {}).get("final_items", 0) for r in records]
        tokens = [r.get("llm", {}).get("total_tokens", 0) for r in records]

        return {
            "total_runs": len(records),
            "avg_cost_usd": round(sum(costs) / len(costs), 6),
            "total_cost_usd": round(sum(costs), 4),
            "avg_items": round(sum(items) / len(items), 1),
            "avg_tokens": round(sum(tokens) / len(tokens)),
            "max_items": max(items) if items else 0,
        }

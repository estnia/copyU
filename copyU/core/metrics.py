# -*- coding: utf-8 -*-
"""
监控指标收集器模块

收集关键路径的延迟、计数器等指标，用于性能监控。
"""

import threading
import time
from typing import Dict, List


class MetricsCollector:
    """简单的监控指标收集器

    收集关键路径的延迟、计数器等指标，用于性能监控。
    """
    def __init__(self):
        self._counters: Dict[str, int] = {}
        self._latency_records: Dict[str, List[float]] = {}
        self._lock = threading.Lock()
        self._max_records_per_key = 1000  # 每个指标最多保留的记录数

    def increment(self, name: str, value: int = 1):
        """增加计数器"""
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + value

    def record_latency(self, name: str, latency_ms: float):
        """记录延迟（毫秒）"""
        with self._lock:
            if name not in self._latency_records:
                self._latency_records[name] = []
            records = self._latency_records[name]
            records.append(latency_ms)
            # 限制记录数量，避免内存无限增长
            if len(records) > self._max_records_per_key:
                self._latency_records[name] = records[-self._max_records_per_key//2:]

    def time_operation(self, name: str):
        """上下文管理器：计时操作"""
        class _TimerContext:
            def __init__(ctx_self, collector, metric_name):
                ctx_self.collector = collector
                ctx_self.metric_name = metric_name
                ctx_self.start_time = None

            def __enter__(ctx_self):
                ctx_self.start_time = time.monotonic()
                return ctx_self

            def __exit__(ctx_self, *args):
                elapsed = (time.monotonic() - ctx_self.start_time) * 1000  # 转换为毫秒
                ctx_self.collector.record_latency(ctx_self.metric_name, elapsed)
                ctx_self.collector.increment(f"{ctx_self.metric_name}_count")

        return _TimerContext(self, name)

    def get_counter(self, name: str) -> int:
        """获取计数器值"""
        with self._lock:
            return self._counters.get(name, 0)

    def get_latency_stats(self, name: str) -> Dict[str, float]:
        """获取延迟统计信息"""
        with self._lock:
            records = self._latency_records.get(name, [])
            if not records:
                return {}
            sorted_records = sorted(records)
            n = len(sorted_records)
            return {
                'count': n,
                'p50': sorted_records[n // 2],
                'p95': sorted_records[int(n * 0.95)],
                'p99': sorted_records[int(n * 0.99)],
                'max': sorted_records[-1],
                'min': sorted_records[0],
            }

    def reset(self):
        """重置所有指标"""
        with self._lock:
            self._counters.clear()
            self._latency_records.clear()


# 全局指标收集器实例
metrics = MetricsCollector()

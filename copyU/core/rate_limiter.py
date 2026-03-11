# -*- coding: utf-8 -*-
"""
速率限制器模块

提供滑动窗口速率限制功能，用于限制高频触发源的调用频率。
"""

import threading
import time
from collections import deque


class RateLimiter:
    """滑动窗口速率限制器

    用于限制高频触发源（剪贴板、快捷键等）的调用频率。
    """
    def __init__(self, max_calls: int, window_seconds: float):
        """
        Args:
            max_calls: 窗口期内最大调用次数
            window_seconds: 窗口大小（秒）
        """
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._calls: deque = deque()
        self._lock = threading.Lock()

    def is_allowed(self) -> bool:
        """检查是否允许此次调用"""
        now = time.monotonic()
        with self._lock:
            # 清理过期的调用记录
            cutoff = now - self.window_seconds
            while self._calls and self._calls[0] < cutoff:
                self._calls.popleft()

            if len(self._calls) < self.max_calls:
                self._calls.append(now)
                return True
            return False

    def get_remaining(self) -> int:
        """获取当前窗口内剩余可用次数"""
        now = time.monotonic()
        with self._lock:
            cutoff = now - self.window_seconds
            while self._calls and self._calls[0] < cutoff:
                self._calls.popleft()
            return max(0, self.max_calls - len(self._calls))

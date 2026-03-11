# -*- coding: utf-8 -*-
"""
线程池模块

管理 QThreadPool 和任务队列，提供任务提交和队列管理功能。
"""

import queue
import threading
from typing import Dict

from PyQt5.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from copyU.core.metrics import metrics
from copyU.infrastructure.logging_config import logger


class WorkerSignals(QObject):
    """Worker 信号定义"""
    finished = pyqtSignal(object, object)  # (result, exception)
    progress = pyqtSignal(int)  # 进度百分比


class Worker(QRunnable):
    """通用工作线程类 - 用于在 QThreadPool 中执行任务

    将函数包装为 QRunnable 以便在 QThreadPool 中执行。
    """
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self.setAutoDelete(True)

    def run(self):
        """执行包装的任务"""
        try:
            with metrics.time_operation(f"worker_{self.fn.__name__}"):
                result = self.fn(*self.args, **self.kwargs)
            self.signals.finished.emit(result, None)
        except Exception as e:
            logger.exception("Worker 执行异常: %s", e)
            self.signals.finished.emit(None, e)


class ThreadPoolManager:
    """线程池管理器

    统一管理 QThreadPool 和任务调度，提供任务提交和队列管理功能。
    """
    def __init__(self, max_threads: int = 4, max_queue_size: int = 500):
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(max_threads)
        self.task_queue = queue.Queue(maxsize=max_queue_size)
        self._dropped_count = 0
        self._lock = threading.Lock()

    def submit(self, fn, *args, callback=None, **kwargs) -> bool:
        """提交任务到线程池

        Args:
            fn: 要执行的函数
            args, kwargs: 函数参数
            callback: 完成回调，接收 (result, exception)

        Returns:
            bool: True 表示成功提交
        """
        try:
            worker = Worker(fn, *args, **kwargs)
            if callback:
                worker.signals.finished.connect(lambda r, e: callback(r, e))
            self.pool.start(worker)
            metrics.increment("threadpool_task_submitted")
            return True
        except Exception as e:
            logger.error("提交任务到线程池失败: %s", e)
            with self._lock:
                self._dropped_count += 1
            metrics.increment("threadpool_task_dropped")
            return False

    def submit_queue_task(self, fn, *args, **kwargs) -> bool:
        """提交任务到队列（带背压）

        Args:
            fn: 要执行的函数
            args, kwargs: 函数参数

        Returns:
            bool: True 表示成功加入队列
        """
        try:
            self.task_queue.put_nowait((fn, args, kwargs))
            metrics.increment("queue_task_submitted")
            return True
        except queue.Full:
            with self._lock:
                self._dropped_count += 1
            if self._dropped_count % 100 == 1:
                logger.warning("任务队列已满，已丢弃 %d 个任务", self._dropped_count)
            metrics.increment("queue_task_dropped")
            return False

    def process_queue(self):
        """处理队列中的任务（应在后台线程中循环调用）"""
        while True:
            try:
                fn, args, kwargs = self.task_queue.get(timeout=1)
                self.submit(fn, *args, **kwargs)
                self.task_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.exception("处理队列任务异常: %s", e)

    def get_stats(self) -> Dict[str, int]:
        """获取线程池统计信息"""
        return {
            'active_threads': self.pool.activeThreadCount(),
            'max_threads': self.pool.maxThreadCount(),
            'queue_size': self.task_queue.qsize(),
            'dropped_tasks': self._dropped_count,
        }

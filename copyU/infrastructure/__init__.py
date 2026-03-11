# -*- coding: utf-8 -*-
"""
copyU 基础设施层模块

包含日志配置、数据库操作、线程池管理等技术实现细节。
"""

from .logging_config import setup_logging, logger
from .database import sqlite_conn, DatabaseWorker, DB_TIMEOUT_SECONDS, DB_BUSY_TIMEOUT_MS, MAX_TASK_QUEUE_SIZE
from .thread_pool import WorkerSignals, Worker, ThreadPoolManager

__all__ = [
    'setup_logging', 'logger',
    'sqlite_conn', 'DatabaseWorker', 'DB_TIMEOUT_SECONDS', 'DB_BUSY_TIMEOUT_MS', 'MAX_TASK_QUEUE_SIZE',
    'WorkerSignals', 'Worker', 'ThreadPoolManager'
]

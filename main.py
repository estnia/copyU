#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
copyU - UOS V20 剪贴板管理工具
基于 PyQt5 + SQLite3 开发
"""

import sys
import os
import sqlite3
import time
import threading
import queue  # 添加 queue 模块用于有界队列
import configparser
import logging
import logging.handlers  # 用于 RotatingFileHandler
import shutil
from collections import deque
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict
from contextlib import contextmanager

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QSystemTrayIcon, QMenu, QAction, QLabel, QAbstractItemView, QTabWidget,
    QTabBar, QPushButton, QInputDialog, QMessageBox, QToolTip
)
from PyQt5.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QMimeData, QPoint, QSize, QObject, QRect, QProcess,
    QRunnable, QThreadPool
)
from PyQt5.QtGui import (
    QClipboard, QIcon, QColor, QPalette, QFont, QKeyEvent, QCursor
)

# 全局热键处理
try:
    from pynput import keyboard
    PYNPUT_AVAILABLE = True
except ImportError:
    PYNPUT_AVAILABLE = False
    logger.warning("pynput 未安装，尝试使用 system_hotkey")

try:
    from system_hotkey import SystemHotkey
    SYSTEM_HOTKEY_AVAILABLE = True
except ImportError:
    SYSTEM_HOTKEY_AVAILABLE = False


# 配置日志 - 添加 RotatingFileHandler 用于错误日志轮转
def setup_logging():
    """设置日志配置，包含控制台输出和错误日志文件轮转"""
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    date_format = '%Y-%m-%d %H:%M:%S'

    # 基础控制台日志配置
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        datefmt=date_format
    )

    # 获取 copyU 根 logger
    copyu_logger = logging.getLogger('copyU')

    # 确保日志目录存在
    log_dir = os.path.expanduser('~/.config/copyu/logs')
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        log_dir = '/tmp/copyu_logs'
        os.makedirs(log_dir, exist_ok=True)

    # 添加错误日志文件处理器（RotatingFileHandler）
    # 单个文件最大 5MB，保留 3 个备份文件
    error_log_path = os.path.join(log_dir, 'error.log')
    error_handler = logging.handlers.RotatingFileHandler(
        error_log_path,
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=3,
        encoding='utf-8'
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter(log_format, date_format))
    copyu_logger.addHandler(error_handler)

    # 添加性能指标日志处理器（可选）
    metrics_log_path = os.path.join(log_dir, 'metrics.log')
    metrics_handler = logging.handlers.RotatingFileHandler(
        metrics_log_path,
        maxBytes=2 * 1024 * 1024,  # 2MB
        backupCount=2,
        encoding='utf-8'
    )
    metrics_handler.setLevel(logging.INFO)
    metrics_handler.setFormatter(logging.Formatter(log_format, date_format))

    # 创建 metrics logger
    metrics_logger = logging.getLogger('copyU.metrics')
    metrics_logger.addHandler(metrics_handler)
    metrics_logger.setLevel(logging.INFO)

    return copyu_logger


# 初始化日志
logger = setup_logging()

# 常量定义
MAX_CLIPBOARD_SIZE = 2_000_000  # 2MB 剪贴板内容大小限制
DB_TIMEOUT_SECONDS = 5  # 数据库连接超时时间
DB_BUSY_TIMEOUT_MS = 3000  # 数据库忙等待超时（毫秒）
MAX_TASK_QUEUE_SIZE = 1000  # 任务队列最大长度


# ==================== 数据库连接上下文管理器 ====================
@contextmanager
def sqlite_conn(db_path: str, timeout: float = DB_TIMEOUT_SECONDS, wal_mode: bool = True):
    """SQLite 数据库连接上下文管理器

    自动处理连接建立、WAL模式设置、事务提交/回滚和连接关闭。
    在异常时自动回滚事务，确保数据一致性。

    Args:
        db_path: 数据库文件路径
        timeout: 连接超时时间（秒）
        wal_mode: 是否启用 WAL 模式

    Yields:
        sqlite3.Connection: 数据库连接对象

    Example:
        with sqlite_conn(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM table")
    """
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=timeout)
        conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS};")
        if wal_mode:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")  # WAL模式下使用NORMAL同步级别平衡性能和安全
        yield conn
        conn.commit()  # 成功时自动提交
    except Exception:
        if conn:
            try:
                conn.rollback()  # 异常时自动回滚
            except sqlite3.Error:
                pass  # 回滚失败时忽略
        raise
    finally:
        if conn:
            try:
                conn.close()
            except sqlite3.Error:
                pass  # 关闭失败时忽略


# ==================== 配置管理 ====================
class ConfigManager:
    """配置文件管理器 - 使用 XDG 规范目录 ~/.config/copyu/"""

    def __init__(self):
        # 使用 XDG Base Directory 规范
        self.config_dir = os.path.expanduser('~/.config/copyu')
        self.CONFIG_FILE = os.path.join(self.config_dir, 'config.ini')
        self.config = configparser.ConfigParser()
        self.load_config()

    def load_config(self):
        """加载配置，如果不存在则创建默认配置"""
        # 确保配置目录存在
        if not os.path.exists(self.config_dir):
            os.makedirs(self.config_dir, mode=0o755, exist_ok=True)

        if os.path.exists(self.CONFIG_FILE):
            self.config.read(self.CONFIG_FILE, encoding='utf-8')
        else:
            self.create_default_config()

    def create_default_config(self):
        """创建默认配置"""
        db_path = os.path.join(self.config_dir, 'clipboard_store.db')
        self.config['General'] = {
            'database_path': db_path,
            'max_age_days': '3',
            'max_record_size_mb': '1',
            'cleanup_interval_hours': '1',
            'hotkey_show': '<ctrl>+grave'  # Ctrl+~ 显示/隐藏剪贴板窗口
        }
        self.config['UI'] = {
            'window_opacity': '0.95',
            'window_width': '400',
            'window_height': '300',
            'max_display_items': '50',
            'font_size': '12'
        }
        self.save_config()

    def save_config(self):
        """保存配置到文件"""
        # 确保配置目录存在
        if not os.path.exists(self.config_dir):
            os.makedirs(self.config_dir, mode=0o755, exist_ok=True)
        with open(self.CONFIG_FILE, 'w', encoding='utf-8') as f:
            self.config.write(f)

    def get(self, section: str, key: str, fallback=None):
        """获取配置值"""
        return self.config.get(section, key, fallback=fallback)

    def getint(self, section: str, key: str, fallback=0):
        """获取整数配置值"""
        return self.config.getint(section, key, fallback=fallback)

    def getfloat(self, section: str, key: str, fallback=0.0):
        """获取浮点数配置值"""
        return self.config.getfloat(section, key, fallback=fallback)

    def set(self, section: str, key: str, value):
        """设置配置值"""
        if section not in self.config:
            self.config[section] = {}
        self.config[section][key] = str(value)

    def setint(self, section: str, key: str, value: int):
        """设置整数配置值"""
        self.set(section, key, value)


# ==================== 监控指标收集器 ====================
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


# 全局指标收集器
metrics = MetricsCollector()


# ==================== 速率限制器 ====================
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


# ==================== QThreadPool Worker 类 ====================
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


# ==================== 数据库操作线程 ====================
class DatabaseWorker(QThread):
    """数据库操作工作线程"""

    # 信号定义
    record_saved = pyqtSignal(int)  # 记录保存成功，返回记录ID
    records_loaded = pyqtSignal(list)  # 记录加载完成
    cleanup_done = pyqtSignal(int)  # 清理完成，返回删除数量
    error_occurred = pyqtSignal(str)  # 错误信号
    # 标签页相关信号
    tabs_loaded = pyqtSignal(list)  # 标签页加载完成 [(id, name, sort_order, is_default), ...]
    tab_records_loaded = pyqtSignal(int, list)  # (tab_id, records)
    tab_created = pyqtSignal(int, str)  # (tab_id, name)
    tab_renamed = pyqtSignal(int, str)  # (tab_id, new_name)
    tab_deleted = pyqtSignal(int)  # (tab_id)
    tabs_reordered = pyqtSignal()  # 标签页重新排序完成
    record_pinned = pyqtSignal(int, int)  # (record_id, tab_id)
    record_unpinned = pyqtSignal(int, int)  # (record_id, tab_id)
    record_moved = pyqtSignal(int, int, int)  # (record_id, from_tab_id, to_tab_id)
    pinned_records_reordered = pyqtSignal(int)  # (tab_id)
    record_deleted = pyqtSignal(int)  # (record_id)

    def __init__(self, db_path: str, max_age_days: int, max_size_mb: int):
        super().__init__()
        self.db_path = db_path
        self.max_age_days = max_age_days
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self.running = True

        # 任务队列 - 使用有界队列防止内存无限增长，队列满时丢弃+告警
        self.task_queue = queue.Queue(maxsize=MAX_TASK_QUEUE_SIZE)
        self.queue_lock = threading.Lock()
        self.queue_event = threading.Event()
        self._dropped_tasks_count = 0  # 记录丢弃的任务数用于监控

        self.init_database()

    def init_database(self):
        """初始化数据库 - 使用统一上下文管理器确保连接正确关闭"""
        with sqlite_conn(self.db_path) as conn:
            cursor = conn.cursor()

            # 剪贴板记录表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS clipboard_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    html_content TEXT,
                    plain_text TEXT,
                    timestamp REAL NOT NULL,
                    app_name TEXT,
                    content_size INTEGER
                )
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_timestamp ON clipboard_records(timestamp)
            ''')

            # 标签页表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS tabs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_order INTEGER DEFAULT 0,
                    is_default INTEGER DEFAULT 0,
                    created_at REAL DEFAULT (strftime('%s', 'now'))
                )
            ''')

            # 固定记录表（记录与标签页的多对多关系）
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS pinned_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL,
                    tab_id INTEGER NOT NULL,
                    sort_order INTEGER DEFAULT 0,
                    pinned_at REAL DEFAULT (strftime('%s', 'now')),
                    FOREIGN KEY (record_id) REFERENCES clipboard_records(id) ON DELETE CASCADE,
                    FOREIGN KEY (tab_id) REFERENCES tabs(id) ON DELETE CASCADE,
                    UNIQUE(record_id, tab_id)
                )
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_pinned_tab ON pinned_records(tab_id, sort_order)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_pinned_record ON pinned_records(record_id)
            ''')

            # 确保默认页存在
            cursor.execute('SELECT id FROM tabs WHERE is_default = 1')
            if not cursor.fetchone():
                cursor.execute('''
                    INSERT INTO tabs (name, sort_order, is_default)
                    VALUES ('默认', 0, 1)
                ''')

    def run(self):
        """主循环，处理数据库任务 - 使用有界队列"""
        while self.running:
            self.queue_event.wait(timeout=60)  # 等待任务或超时
            self.queue_event.clear()

            if not self.running:
                break

            # 处理任务队列 - 使用 queue.Queue 的 get 方法
            tasks = []
            with self.queue_lock:
                # 批量取出所有当前任务
                while not self.task_queue.empty():
                    try:
                        tasks.append(self.task_queue.get_nowait())
                    except queue.Empty:
                        break

            for task in tasks:
                if not self.running:
                    break
                try:
                    logger.debug("执行任务: %s", task.get('type'))
                    self.execute_task(task)
                    logger.debug("任务完成: %s", task.get('type'))
                except Exception as e:
                    logger.exception("任务执行异常: %s", e)
                    self.error_occurred.emit(str(e))

            # 定期清理
            if self.running:
                self.cleanup_old_records()

    def execute_task(self, task: Dict):
        """执行单个任务"""
        task_type = task.get('type')

        if task_type == 'save':
            self._save_record(task)
        elif task_type == 'load':
            self._load_records(task)
        elif task_type == 'cleanup':
            self._cleanup_records()
        elif task_type == 'clear_all':
            self._clear_all_default_records()
        elif task_type == 'load_tabs':
            self._load_tabs(task)
        elif task_type == 'load_tab_records':
            self._load_tab_records(task)
        elif task_type == 'create_tab':
            self._create_tab(task)
        elif task_type == 'rename_tab':
            self._rename_tab(task)
        elif task_type == 'delete_tab':
            self._delete_tab(task)
        elif task_type == 'reorder_tabs':
            self._reorder_tabs(task)
        elif task_type == 'pin_record':
            self._pin_record(task)
        elif task_type == 'unpin_record':
            self._unpin_record(task)
        elif task_type == 'move_pinned_record':
            self._move_pinned_record(task)
        elif task_type == 'reorder_pinned_records':
            self._reorder_pinned_records(task)
        elif task_type == 'delete_record':
            self._delete_record(task)

    def _save_record(self, task: Dict):
        """保存记录到数据库 - 使用 context manager"""
        html_content = task.get('html_content', '')
        plain_text = task.get('plain_text', '')
        app_name = task.get('app_name', '')

        # 检查内容大小
        content_size = len(html_content.encode('utf-8')) + len(plain_text.encode('utf-8'))
        if content_size > self.max_size_bytes:
            self.error_occurred.emit(f"内容超过 {self.max_size_bytes // 1024 // 1024}MB 限制，跳过保存")
            return

        # 检查是否重复（最近1分钟内相同内容）
        with sqlite_conn(self.db_path) as conn:
            cursor = conn.cursor()

            one_minute_ago = time.time() - 60
            cursor.execute('''
                SELECT id FROM clipboard_records
                WHERE plain_text = ? AND timestamp > ?
            ''', (plain_text, one_minute_ago))

            if cursor.fetchone():
                return  # 重复内容，跳过

            # 插入新记录
            cursor.execute('''
                INSERT INTO clipboard_records (html_content, plain_text, timestamp, app_name, content_size)
                VALUES (?, ?, ?, ?, ?)
            ''', (html_content, plain_text, time.time(), app_name, content_size))

            record_id = cursor.lastrowid

        self.record_saved.emit(record_id)

    def _load_records(self, task: Dict):
        """从数据库加载记录 - 使用统一上下文管理器"""
        limit = task.get('limit', 50)
        search_text = task.get('search', '')

        with sqlite_conn(self.db_path) as conn:
            cursor = conn.cursor()

            if search_text:
                cursor.execute('''
                    SELECT id, html_content, plain_text, timestamp, app_name
                    FROM clipboard_records
                    WHERE plain_text LIKE ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                ''', (f'%{search_text}%', limit))
            else:
                cursor.execute('''
                    SELECT id, html_content, plain_text, timestamp, app_name
                    FROM clipboard_records
                    ORDER BY timestamp DESC
                    LIMIT ?
                ''', (limit,))

            records = cursor.fetchall()

        # 转换为字典列表
        result = []
        for row in records:
            result.append({
                'id': row[0],
                'html_content': row[1] or '',
                'plain_text': row[2] or '',
                'timestamp': row[3],
                'app_name': row[4] or '',
                'display_time': datetime.fromtimestamp(row[3]).strftime('%m-%d %H:%M')
            })

        self.records_loaded.emit(result)

    def _cleanup_records(self):
        """清理过期记录 - 保留被固定到自定义标签页的记录，使用 context manager"""
        cutoff_time = time.time() - (self.max_age_days * 24 * 3600)

        with sqlite_conn(self.db_path) as conn:
            cursor = conn.cursor()

            # 只删除未被固定到任何自定义标签页的过期记录
            # 被固定的记录将永久保留
            cursor.execute('''
                DELETE FROM clipboard_records
                WHERE timestamp < ?
                AND id NOT IN (
                    SELECT DISTINCT record_id FROM pinned_records
                )
            ''', (cutoff_time,))

            deleted_count = cursor.rowcount

        if deleted_count > 0:
            self.cleanup_done.emit(deleted_count)

    def _clear_all_default_records(self):
        """清理所有默认页记录（未固定的） - 使用 context manager"""
        logger.info("开始执行 _clear_all_default_records")
        try:
            with sqlite_conn(self.db_path) as conn:
                cursor = conn.cursor()

                # 先查询有多少记录将被删除
                cursor.execute('''
                    SELECT COUNT(*) FROM clipboard_records
                    WHERE id NOT IN (
                        SELECT DISTINCT record_id FROM pinned_records
                    )
                ''')
                count_to_delete = cursor.fetchone()[0]
                logger.info("准备删除 %s 条记录", count_to_delete)

                # 只删除未被固定到任何自定义标签页的记录
                cursor.execute('''
                    DELETE FROM clipboard_records
                    WHERE id NOT IN (
                        SELECT DISTINCT record_id FROM pinned_records
                    )
                ''')

                deleted_count = cursor.rowcount
                logger.info("实际删除 %s 条记录", deleted_count)

            if deleted_count > 0:
                logger.info("发送 cleanup_done 信号: %s", deleted_count)
                self.cleanup_done.emit(deleted_count)
            else:
                logger.info("没有记录被删除，不发送信号")
            logger.info("_clear_all_default_records 完成")
        except Exception as e:
            logger.exception("_clear_all_default_records 异常: %s", e)
            raise

    def _load_tabs(self, task: Dict):
        """加载所有标签页 - 使用 context manager"""
        with sqlite_conn(self.db_path) as conn:
            cursor = conn.cursor()

            cursor.execute('''
                SELECT id, name, sort_order, is_default
                FROM tabs
                ORDER BY is_default DESC, sort_order ASC, id ASC
            ''')

            tabs = cursor.fetchall()

        result = []
        for row in tabs:
            result.append({
                'id': row[0],
                'name': row[1],
                'sort_order': row[2],
                'is_default': bool(row[3])
            })

        self.tabs_loaded.emit(result)

    def _load_tab_records(self, task: Dict):
        """加载指定标签页的记录 - 使用 context manager"""
        tab_id = task.get('tab_id')
        limit = task.get('limit', 50)

        with sqlite_conn(self.db_path) as conn:
            cursor = conn.cursor()

            # 检查是否是默认页
            cursor.execute('SELECT is_default FROM tabs WHERE id = ?', (tab_id,))
            result = cursor.fetchone()
            is_default = result and result[0] == 1

            # 获取被固定到任意自定义标签页的记录ID（用于默认页显示蓝色）
            pinned_record_ids = set()
            if is_default:
                cursor.execute('''
                    SELECT DISTINCT record_id FROM pinned_records
                ''')
                pinned_record_ids = set(row[0] for row in cursor.fetchall())

            if is_default:
                # 默认页显示所有记录（按时间倒序）
                cursor.execute('''
                    SELECT id, html_content, plain_text, timestamp, app_name
                    FROM clipboard_records
                    ORDER BY timestamp DESC
                    LIMIT ?
                ''', (limit,))
            else:
                # 自定义标签页显示固定的记录（按固定排序，最新的排最前）
                cursor.execute('''
                    SELECT r.id, r.html_content, r.plain_text, r.timestamp, r.app_name
                    FROM clipboard_records r
                    JOIN pinned_records p ON r.id = p.record_id
                    WHERE p.tab_id = ?
                    ORDER BY p.sort_order DESC
                    LIMIT ?
                ''', (tab_id, limit))

            records = cursor.fetchall()

        result = []
        for row in records:
            result.append({
                'id': row[0],
                'html_content': row[1] or '',
                'plain_text': row[2] or '',
                'timestamp': row[3],
                'app_name': row[4] or '',
                'display_time': datetime.fromtimestamp(row[3]).strftime('%m-%d %H:%M'),
                'is_pinned': row[0] in pinned_record_ids  # 标记是否被固定
            })

        self.tab_records_loaded.emit(tab_id, result)

    def _create_tab(self, task: Dict):
        """创建新标签页 - 使用 context manager"""
        name = task.get('name', '新标签页')

        with sqlite_conn(self.db_path) as conn:
            cursor = conn.cursor()

            # 获取最大排序值
            cursor.execute('SELECT MAX(sort_order) FROM tabs WHERE is_default = 0')
            max_order = cursor.fetchone()[0] or 0

            cursor.execute('''
                INSERT INTO tabs (name, sort_order, is_default)
                VALUES (?, ?, 0)
            ''', (name, max_order + 1))

            tab_id = cursor.lastrowid

        self.tab_created.emit(tab_id, name)

    def _rename_tab(self, task: Dict):
        """重命名标签页 - 使用 context manager"""
        tab_id = task.get('tab_id')
        name = task.get('name', '')

        with sqlite_conn(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE tabs SET name = ? WHERE id = ? AND is_default = 0', (name, tab_id))

        self.tab_renamed.emit(tab_id, name)

    def _delete_tab(self, task: Dict):
        """删除标签页（同时删除关联的固定记录）- 使用 context manager"""
        tab_id = task.get('tab_id')

        with sqlite_conn(self.db_path) as conn:
            cursor = conn.cursor()

            # 先删除关联的固定记录
            cursor.execute('DELETE FROM pinned_records WHERE tab_id = ?', (tab_id,))

            # 再删除标签页
            cursor.execute('DELETE FROM tabs WHERE id = ? AND is_default = 0', (tab_id,))

        self.tab_deleted.emit(tab_id)

    def _reorder_tabs(self, task: Dict):
        """重新排序标签页 - 使用 context manager"""
        tab_orders = task.get('tab_orders', [])  # [(tab_id, new_order), ...]

        with sqlite_conn(self.db_path) as conn:
            cursor = conn.cursor()

            for tab_id, new_order in tab_orders:
                cursor.execute('UPDATE tabs SET sort_order = ? WHERE id = ? AND is_default = 0', (new_order, tab_id))

        self.tabs_reordered.emit()

    def _pin_record(self, task: Dict):
        """固定记录到标签页 - 使用 context manager"""
        record_id = task.get('record_id')
        tab_id = task.get('tab_id')

        with sqlite_conn(self.db_path) as conn:
            cursor = conn.cursor()

            # 获取最大排序值
            cursor.execute('SELECT MAX(sort_order) FROM pinned_records WHERE tab_id = ?', (tab_id,))
            max_order = cursor.fetchone()[0] or 0

            cursor.execute('''
                INSERT OR IGNORE INTO pinned_records (record_id, tab_id, sort_order)
                VALUES (?, ?, ?)
            ''', (record_id, tab_id, max_order + 1))

        self.record_pinned.emit(record_id, tab_id)

    def _unpin_record(self, task: Dict):
        """从标签页移除固定记录 - 使用 context manager"""
        record_id = task.get('record_id')
        tab_id = task.get('tab_id')

        with sqlite_conn(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM pinned_records WHERE record_id = ? AND tab_id = ?', (record_id, tab_id))

        self.record_unpinned.emit(record_id, tab_id)

    def _delete_record(self, task: Dict):
        """删除记录 - 使用 context manager"""
        record_id = task.get('record_id')

        with sqlite_conn(self.db_path) as conn:
            cursor = conn.cursor()
            # 先从 pinned_records 中删除关联
            cursor.execute('DELETE FROM pinned_records WHERE record_id = ?', (record_id,))
            # 再删除记录本身
            cursor.execute('DELETE FROM clipboard_records WHERE id = ?', (record_id,))

        self.record_deleted.emit(record_id)

    def _move_pinned_record(self, task: Dict):
        """移动固定记录到其他标签页 - 使用 context manager"""
        record_id = task.get('record_id')
        from_tab_id = task.get('from_tab_id')
        to_tab_id = task.get('to_tab_id')

        with sqlite_conn(self.db_path) as conn:
            cursor = conn.cursor()

            # 从原标签页删除
            cursor.execute('DELETE FROM pinned_records WHERE record_id = ? AND tab_id = ?', (record_id, from_tab_id))

            # 获取目标标签页的最大排序值
            cursor.execute('SELECT MAX(sort_order) FROM pinned_records WHERE tab_id = ?', (to_tab_id,))
            max_order = cursor.fetchone()[0] or 0

            # 插入到新标签页
            cursor.execute('''
                INSERT OR IGNORE INTO pinned_records (record_id, tab_id, sort_order)
                VALUES (?, ?, ?)
            ''', (record_id, to_tab_id, max_order + 1))

        self.record_moved.emit(record_id, from_tab_id, to_tab_id)

    def _reorder_pinned_records(self, task: Dict):
        """重新排序固定记录 - 使用 context manager"""
        tab_id = task.get('tab_id')
        record_orders = task.get('record_orders', [])  # [(record_id, new_order), ...]

        with sqlite_conn(self.db_path) as conn:
            cursor = conn.cursor()

            for record_id, new_order in record_orders:
                cursor.execute('UPDATE pinned_records SET sort_order = ? WHERE record_id = ? AND tab_id = ?',
                             (new_order, record_id, tab_id))

        self.pinned_records_reordered.emit(tab_id)

    def cleanup_old_records(self):
        """公开方法：触发清理"""
        self._cleanup_records()

    def add_task(self, task: Dict) -> bool:
        """添加任务到队列 - 带背压处理

        Args:
            task: 任务字典

        Returns:
            bool: True 表示成功加入队列，False 表示队列已满丢弃任务
        """
        with self.queue_lock:
            try:
                self.task_queue.put_nowait(task)
                self.queue_event.set()
                return True
            except queue.Full:
                self._dropped_tasks_count += 1
                # 每丢弃100个任务记录一次警告，避免日志刷屏
                if self._dropped_tasks_count % 100 == 1:
                    logger.warning(
                        "任务队列已满 (maxsize=%d)，丢弃新任务 [%s]。累计丢弃: %d",
                        MAX_TASK_QUEUE_SIZE, task.get('type', 'unknown'), self._dropped_tasks_count
                    )
                return False

    def stop(self):
        """停止线程"""
        self.running = False
        self.queue_event.set()
        self.wait()


# ==================== 剪贴板管理器 ====================
class ClipboardManager:
    """剪贴板内容管理"""

    def __init__(self, clipboard: QClipboard):
        self.clipboard = clipboard
        self.last_content = ''
        self._last_clipboard = None  # 用于避免重复设置剪贴板

    def get_content(self) -> Tuple[str, str]:
        """获取剪贴板内容，返回 (html, plain_text)"""
        mime_data = self.clipboard.mimeData()

        html_content = ''
        plain_text = ''

        # 获取 HTML 格式
        if mime_data.hasHtml():
            html_content = mime_data.html()

        # 获取纯文本格式
        if mime_data.hasText():
            plain_text = mime_data.text()

        return html_content, plain_text

    def set_content(self, html: str = '', plain_text: str = ''):
        """设置剪贴板内容 - 避免重复设置导致UI抖动"""
        # 检查是否与上次设置的内容相同
        current_content = (html, plain_text)
        if getattr(self, '_last_clipboard', None) == current_content:
            return  # 重复内容，跳过设置

        mime_data = QMimeData()

        if html:
            mime_data.setHtml(html)
        if plain_text:
            mime_data.setText(plain_text)

        self.clipboard.setMimeData(mime_data)
        self._last_clipboard = current_content

    def is_duplicate(self, content: str) -> bool:
        """检查内容是否与上次相同"""
        if content == self.last_content:
            return True
        self.last_content = content
        return False


# ==================== 粘贴窗口 ====================
class PasteWindow(QWidget):
    """选择性粘贴窗口 - 支持多标签页"""

    paste_requested = pyqtSignal(int, bool)  # (record_id, is_plain_text)
    closed = pyqtSignal()
    # 标签页相关信号
    load_tabs_requested = pyqtSignal()  # 请求加载标签页列表
    load_tab_records_requested = pyqtSignal(int)  # 请求加载指定标签页记录 (tab_id)
    create_tab_requested = pyqtSignal(str)  # 请求创建新标签页 (name)
    rename_tab_requested = pyqtSignal(int, str)  # 请求重命名标签页 (tab_id, name)
    delete_tab_requested = pyqtSignal(int)  # 请求删除标签页 (tab_id)
    reorder_tabs_requested = pyqtSignal(list)  # 请求重新排序标签页 [(tab_id, order), ...]
    pin_record_requested = pyqtSignal(int, int)  # 请求固定记录 (record_id, tab_id)
    unpin_record_requested = pyqtSignal(int, int)  # 请求取消固定 (record_id, tab_id)
    delete_record_requested = pyqtSignal(int)  # 请求删除记录 (record_id)
    move_record_requested = pyqtSignal(int, int, int)  # 请求移动记录 (record_id, from_tab_id, to_tab_id)
    reorder_records_requested = pyqtSignal(int, list)  # 请求重新排序固定记录 (tab_id, [(record_id, order), ...])
    # 批量操作信号
    batch_pin_requested = pyqtSignal(list, int)  # (record_ids[], target_tab_id)
    batch_unpin_requested = pyqtSignal(list, int)  # (record_ids[], current_tab_id)
    batch_delete_requested = pyqtSignal(list)  # (record_ids[])

    def __init__(self, config: ConfigManager, parent=None):
        super().__init__(parent)
        self.config = config
        self.tabs = []  # 标签页列表 [(id, name, is_default), ...]
        self.current_records = {}  # 当前各标签页的记录 {tab_id: [records]}
        self.current_tab_id = None  # 当前选中标签页ID
        self.dragged_record_id = None  # 当前拖动的记录ID
        self.dragged_from_tab_id = None  # 拖动来源标签页ID
        self._tab_menu = None  # 当前打开的标签页菜单
        self._tab_selection = {}  # 保存每个标签页的选择位置 {tab_id: row}

        self.setup_ui()
        self.setup_style()

        # 安装事件过滤器用于检测点击外部
        self.installEventFilter(self)

    def setup_ui(self):
        """设置UI界面"""
        # 窗口属性 - 无边框、置顶、接受焦点
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)

        # 窗口大小和位置 - 支持自由调整大小
        width = self.config.getint('UI', 'window_width', 400)
        height = self.config.getint('UI', 'window_height', 300)

        # 设置最小和最大尺寸限制
        self.setMinimumSize(300, 200)   # 最小 300x200
        self.setMaximumSize(800, 600)   # 最大 800x600
        self.resize(width, height)

        # 启用鼠标跟踪用于拖动调整大小
        self.setMouseTracking(True)
        self._resize_edge = None
        self._resize_start_pos = None
        self._resize_start_geometry = None
        self._resize_margin = 8  # 边缘检测宽度

        # 主布局
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 0)
        layout.setSpacing(5)

        font_size = self.config.getint('UI', 'font_size', 12)

        # 标签页控件
        from PyQt5.QtWidgets import QTabWidget, QPushButton, QInputDialog, QMessageBox
        self.tab_widget = QTabWidget()
        self.tab_widget.setFont(QFont('Noto Sans CJK SC', font_size))
        # 关闭系统默认的关闭按钮，改用右键菜单
        self.tab_widget.setTabsClosable(False)
        self.tab_widget.setMovable(True)  # 允许拖动排序
        self.tab_widget.currentChanged.connect(self.on_tab_changed)
        self.tab_widget.tabBar().tabMoved.connect(self.on_tab_moved)

        # 启用标签栏右键菜单
        self.tab_widget.tabBar().setContextMenuPolicy(Qt.CustomContextMenu)
        self.tab_widget.tabBar().customContextMenuRequested.connect(self.on_tab_bar_context_menu)

        # 禁用 expanding，让样式表完全控制标签宽度
        self.tab_widget.tabBar().setExpanding(False)

        # 禁用标签栏滚动按钮，强制所有标签显示在一行
        self.tab_widget.tabBar().setUsesScrollButtons(False)

        # 菜单互斥锁
        self._tab_menu_open = False

        # 移除角落的添加按钮，改用右键菜单
        self.add_tab_button = None

        layout.addWidget(self.tab_widget)

        # 设置标签页样式 - 初始样式，标签宽度将在 update_tabs 中动态调整
        self.tab_widget.setStyleSheet(f'''
            QTabWidget::pane {{
                border: none;
                background: transparent;
            }}
            QTabBar::tab {{
                background: rgba(220, 220, 220, 180);
                border-radius: 4px 4px 0 0;
                padding: 6px 4px;
                margin: 0px;
                font-size: {font_size}px;
                color: #333;
                min-width: 60px;
            }}
            QTabBar::tab:selected {{
                background: rgba(0, 120, 215, 180);
                color: white;
            }}
            QTabBar::tab:hover:!selected {{
                background: rgba(200, 200, 200, 220);
            }}
            QTabBar::close-button {{
                image: none;
                width: 14px;
                height: 14px;
                margin-left: 4px;
            }}
            QTabBar::close-button:hover {{
                background: rgba(255, 100, 100, 180);
                border-radius: 2px;
            }}
        ''')

        # 底部融合栏（快捷键提示）
        bottom_bar = QWidget()
        bottom_bar.setObjectName('bottomBar')
        bottom_layout = QHBoxLayout(bottom_bar)
        bottom_layout.setContentsMargins(10, 6, 10, 6)
        bottom_layout.setSpacing(5)

        # 延迟更新标签宽度的定时器（放在布局后面初始化）
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._update_tab_widths)

        # 快捷键提示标签
        self.title_label = QLabel('双击/Enter粘贴 | Shift+Enter/Ctrl+1纯文本 | ↑↓选择 | ←→切标签 | Ctrl+~显隐 | Ctrl/Shift+单击多选 | 右键管理 | Esc关闭')
        self.title_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.title_label.setFont(QFont('Noto Sans CJK SC', font_size - 2))
        bottom_layout.addWidget(self.title_label)

        # 底部栏滚动效果
        self._scroll_timer = QTimer(self)
        self._scroll_timer.timeout.connect(self._scroll_title_text)
        self._scroll_pos = 0
        self._original_title_text = self.title_label.text()
        bottom_bar.enterEvent = self._on_bottom_bar_enter
        bottom_bar.leaveEvent = self._on_bottom_bar_leave

        layout.addWidget(bottom_bar)

        # 设置透明度
        opacity = self.config.getfloat('UI', 'window_opacity', 0.95)
        self.setWindowOpacity(opacity)

    def setup_style(self):
        """设置UOS深度风格样式"""
        # 设置全局 Tooltip 样式 - 增加对比度，宽度自适应
        QToolTip.setFont(QFont('Noto Sans CJK SC', 11))

        self.setStyleSheet('''
            QWidget {
                background-color: rgba(245, 245, 245, 240);
                border-radius: 8px;
            }
            QLabel {
                color: #666666;
                background-color: transparent;
            }
            QWidget#bottomBar {
                background-color: rgba(235, 235, 235, 200);
                border-bottom-left-radius: 8px;
                border-bottom-right-radius: 8px;
            }
            QToolTip {
                background-color: rgba(255, 255, 255, 250);
                color: #000000;
                border: 1px solid rgba(180, 180, 180, 200);
                border-radius: 4px;
                padding: 8px;
                font-size: 13px;
            }
            QMenu {
                background-color: rgba(245, 245, 245, 240);
                border: 1px solid rgba(200, 200, 200, 180);
                border-radius: 4px;
                padding: 4px;
            }
            QMenu::item {
                color: #333333;
                background-color: transparent;
                border-radius: 4px;
                padding: 6px 20px;
            }
            QMenu::item:selected {
                background-color: rgba(0, 120, 215, 180);
                color: white;
            }
            QMenu::item:disabled {
                color: #999999;
                background-color: transparent;
            }
            QMenu::separator {
                height: 1px;
                background-color: rgba(200, 200, 200, 180);
                margin: 4px 0px;
            }
        ''')

    def create_list_widget_for_tab(self, tab_id: int) -> QListWidget:
        """为标签页创建列表控件"""
        font_size = self.config.getint('UI', 'font_size', 12)

        list_widget = QListWidget()
        list_widget.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        list_widget.setFont(QFont('Noto Sans CJK SC', font_size))
        list_widget.setFocusPolicy(Qt.StrongFocus)
        list_widget.setDragDropMode(QAbstractItemView.InternalMove)  # 允许内部拖动
        list_widget.setDefaultDropAction(Qt.MoveAction)
        list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        list_widget.setContextMenuPolicy(Qt.CustomContextMenu)

        # 设置行高以适应字号（字号+12像素作为最小高度）
        item_height = max(28, font_size + 14)
        list_widget.setStyleSheet(f'''
            QListWidget::item {{
                min-height: {item_height}px;
                padding: 2px;
            }}
            QListWidget::item:selected {{
                background-color: rgba(0, 120, 215, 180);
            }}
        ''')

        # 连接信号
        list_widget.itemDoubleClicked.connect(lambda item, tid=tab_id: self.on_item_double_clicked(item, tid))
        list_widget.customContextMenuRequested.connect(lambda pos, tid=tab_id: self.on_list_context_menu(pos, tid))

        # 安装事件过滤器处理Delete键（列表有焦点时也需要响应Delete）
        list_widget.installEventFilter(self)

        # 启用拖拽
        list_widget.setDragEnabled(True)
        list_widget.setAcceptDrops(True)
        list_widget.viewport().setAcceptDrops(True)
        list_widget.setDropIndicatorShown(True)

        # 安装事件过滤器处理拖拽（暂时禁用，可能导致崩溃）
        # list_widget.model().rowsMoved.connect(
        #     lambda src_parent, src_start, src_end, dst_parent, dst_row, tid=tab_id:
        #     self.on_rows_moved(tid, src_start, dst_row)
        # )

        return list_widget

    def on_rows_moved(self, tab_id: int, source_row: int, dest_row: int):
        """处理列表项拖动排序"""
        if tab_id not in [t['id'] for t in self.tabs if not t['is_default']]:
            return  # 默认页不支持排序

        list_widget = self.get_list_widget_for_tab(tab_id)
        if not list_widget:
            return

        # 构建新的排序列表
        records = self.current_records.get(tab_id, [])
        record_orders = []
        for i in range(list_widget.count()):
            item = list_widget.item(i)
            record_id = item.data(Qt.UserRole)
            record_orders.append((record_id, i))

        # 发送重新排序请求
        self.reorder_records_requested.emit(tab_id, record_orders)

    def get_tab_data(self, index: int) -> Optional[int]:
        """安全获取标签页数据（转换为int）"""
        try:
            tab_bar = self.tab_widget.tabBar()
            if tab_bar is None:
                return None
            data = tab_bar.tabData(index)
            if data is not None:
                return int(data)
        except (AttributeError, TypeError):
            pass
        return None

    def get_list_widget_for_tab(self, tab_id: int) -> Optional[QListWidget]:
        """获取指定标签页的列表控件"""
        try:
            for i in range(self.tab_widget.count()):
                if self.get_tab_data(i) == tab_id:
                    return self.tab_widget.widget(i)
        except (AttributeError, TypeError):
            pass
        return None

    def on_list_context_menu(self, position, tab_id: int):
        """列表右键菜单 - 支持批量操作"""
        list_widget = self.get_list_widget_for_tab(tab_id)
        if not list_widget:
            return

        item = list_widget.itemAt(position)
        if not item:
            return

        # 如果点击的项目未被选中，则清除其他选择并选中当前项
        if not item.isSelected():
            list_widget.clearSelection()
            item.setSelected(True)

        # 获取所有选中的项目
        selected_items = list_widget.selectedItems()
        selected_record_ids = [item.data(Qt.UserRole) for item in selected_items]
        is_batch = len(selected_record_ids) > 1

        menu = QMenu(self)
        font_size = self.config.getint('UI', 'font_size', 12)
        menu.setFont(QFont('Noto Sans CJK SC', font_size))

        # 获取当前标签页信息
        current_tab = next((t for t in self.tabs if t['id'] == tab_id), None)

        if is_batch:
            # 批量操作模式
            self._setup_batch_context_menu(menu, selected_record_ids, current_tab, tab_id, font_size)
        else:
            # 单项目操作模式（原有逻辑）
            record_id = selected_record_ids[0] if selected_record_ids else item.data(Qt.UserRole)
            self._setup_single_context_menu(menu, record_id, current_tab, tab_id, font_size)

        menu.exec_(list_widget.mapToGlobal(position))

    def _setup_single_context_menu(self, menu: QMenu, record_id: int, current_tab: Optional[Dict], tab_id: int, font_size: int):
        """设置单项目右键菜单"""
        # 粘贴选项
        paste_action = QAction('粘贴原格式', self)
        paste_action.triggered.connect(lambda: self.paste_requested.emit(record_id, False))
        menu.addAction(paste_action)

        paste_text_action = QAction('粘贴纯文本', self)
        paste_text_action.triggered.connect(lambda: self.paste_requested.emit(record_id, True))
        menu.addAction(paste_text_action)

        menu.addSeparator()

        if current_tab and current_tab['is_default']:
            # 默认页：显示固定到其他标签页选项
            pin_menu = QMenu('固定到', self)
            pin_menu.setFont(QFont('Noto Sans CJK SC', font_size))

            custom_tabs = [t for t in self.tabs if not t['is_default']]
            if custom_tabs:
                for tab in custom_tabs:
                    action = QAction(tab['name'], self)
                    action.triggered.connect(lambda checked, tid=tab['id']: self.pin_record_requested.emit(record_id, tid))
                    pin_menu.addAction(action)
            else:
                no_tab_action = QAction('(无自定义标签页)', self)
                no_tab_action.setEnabled(False)
                pin_menu.addAction(no_tab_action)

            menu.addMenu(pin_menu)

            menu.addSeparator()

            # 默认页：删除记录（无须确认）
            delete_action = QAction('删除', self)
            delete_action.triggered.connect(lambda: self.delete_record_requested.emit(record_id))
            menu.addAction(delete_action)
        else:
            # 自定义标签页：显示移动到其他标签页和移除选项
            move_menu = QMenu('移动到', self)
            move_menu.setFont(QFont('Noto Sans CJK SC', font_size))

            other_tabs = [t for t in self.tabs if not t['is_default'] and t['id'] != tab_id]
            if other_tabs:
                for tab in other_tabs:
                    action = QAction(tab['name'], self)
                    action.triggered.connect(lambda checked, to_tid=tab['id']: self.move_record_requested.emit(record_id, tab_id, to_tid))
                    move_menu.addAction(action)
            else:
                no_tab_action = QAction('(无其他标签页)', self)
                no_tab_action.setEnabled(False)
                move_menu.addAction(no_tab_action)

            menu.addMenu(move_menu)

            menu.addSeparator()

            # 从当前标签页移除
            unpin_action = QAction('从标签页移除', self)
            import functools
            unpin_action.triggered.connect(
                functools.partial(self._confirm_unpin, record_id, tab_id)
            )
            menu.addAction(unpin_action)

    def _setup_batch_context_menu(self, menu: QMenu, record_ids: List[int], current_tab: Optional[Dict], tab_id: int, font_size: int):
        """设置批量操作右键菜单"""
        count = len(record_ids)

        # 显示选中数量
        info_action = QAction(f'已选择 {count} 项', self)
        info_action.setEnabled(False)
        menu.addAction(info_action)
        menu.addSeparator()

        if current_tab and current_tab['is_default']:
            # 默认页：批量固定到其他标签页
            pin_menu = QMenu('批量固定到', self)
            pin_menu.setFont(QFont('Noto Sans CJK SC', font_size))

            custom_tabs = [t for t in self.tabs if not t['is_default']]
            if custom_tabs:
                for tab in custom_tabs:
                    action = QAction(tab['name'], self)
                    action.triggered.connect(lambda checked, tid=tab['id']: self.batch_pin_requested.emit(record_ids, tid))
                    pin_menu.addAction(action)
            else:
                no_tab_action = QAction('(无自定义标签页)', self)
                no_tab_action.setEnabled(False)
                pin_menu.addAction(no_tab_action)

            menu.addMenu(pin_menu)

            menu.addSeparator()

            # 批量删除
            delete_action = QAction(f'批量删除 ({count} 项)', self)
            delete_action.triggered.connect(lambda: self._confirm_batch_delete(record_ids))
            menu.addAction(delete_action)
        else:
            # 自定义标签页：批量移动和批量移除
            move_menu = QMenu('批量移动到', self)
            move_menu.setFont(QFont('Noto Sans CJK SC', font_size))

            other_tabs = [t for t in self.tabs if not t['is_default'] and t['id'] != tab_id]
            if other_tabs:
                for tab in other_tabs:
                    action = QAction(tab['name'], self)
                    action.triggered.connect(lambda checked, to_tid=tab['id']: self._batch_move_records(record_ids, tab_id, to_tid))
                    move_menu.addAction(action)
            else:
                no_tab_action = QAction('(无其他标签页)', self)
                no_tab_action.setEnabled(False)
                move_menu.addAction(no_tab_action)

            menu.addMenu(move_menu)

            menu.addSeparator()

            # 批量从当前标签页移除
            unpin_action = QAction(f'批量从标签页移除 ({count} 项)', self)
            unpin_action.triggered.connect(lambda: self._confirm_batch_unpin(record_ids, tab_id))
            menu.addAction(unpin_action)

    def _confirm_batch_unpin(self, record_ids: List[int], tab_id: int):
        """确认后批量取消固定记录"""
        count = len(record_ids)
        title = '确认取消固定' if count == 1 else '确认批量取消固定'
        text = (
            f'确定要从当前标签页移除选中的 {count} 条记录吗？\n移除后记录将回到默认页。'
            if count > 1 else
            '确定要从当前标签页移除此记录吗？\n移除后记录将回到默认页。'
        )
        if self._show_confirm_dialog(title, text):
            self.batch_unpin_requested.emit(record_ids, tab_id)

    def _confirm_batch_delete(self, record_ids: List[int]):
        """确认后批量删除记录"""
        count = len(record_ids)
        title = '确认删除' if count == 1 else '确认批量删除'
        text = (
            f'确定要永久删除选中的 {count} 条记录吗？\n此操作不可恢复。'
            if count > 1 else
            '确定要永久删除此记录吗？\n此操作不可恢复。'
        )
        if self._show_confirm_dialog(title, text):
            self.batch_delete_requested.emit(record_ids)

    def _batch_move_records(self, record_ids: List[int], from_tab_id: int, to_tab_id: int):
        """批量移动记录到其他标签页"""
        # 逐个发送移动请求
        for record_id in record_ids:
            self.move_record_requested.emit(record_id, from_tab_id, to_tab_id)

    def on_tab_close_requested(self, index: int):
        """标签页关闭请求"""
        try:
            tab_id = self.get_tab_data(index)
            if tab_id is None:
                return

            tab = next((t for t in self.tabs if t['id'] == tab_id), None)

            if tab and tab['is_default']:
                return  # 默认页不能关闭

            # 获取标签页名称
            try:
                tab_name = self.tab_widget.tabText(index)
            except (AttributeError, TypeError):
                tab_name = "未知"

            # 确认删除
            if self._show_confirm_dialog(
                '确认删除',
                f'确定要删除标签页 "{tab_name}" 吗？\n其中的记录将回到默认页。'
            ):
                self.delete_tab_requested.emit(tab_id)
        except (AttributeError, TypeError) as e:
            print(f"标签页关闭请求错误: {e}")

    def on_tab_changed(self, index: int):
        """标签页切换 - 保留选择光标位置并设置焦点"""
        if index < 0:
            return
        try:
            # 保存当前标签页的光标位置
            if self.current_tab_id is not None:
                old_list_widget = self.get_list_widget_for_tab(self.current_tab_id)
                if old_list_widget:
                    self._tab_cursor_positions[self.current_tab_id] = old_list_widget.currentRow()

            # 切换到新标签页
            self.current_tab_id = self.get_tab_data(index)
            if self.current_tab_id is not None:
                self.load_tab_records_requested.emit(self.current_tab_id)
                # 恢复新标签页的光标位置
                saved_row = self._tab_cursor_positions.get(self.current_tab_id, 0)
                # 在记录加载完成后再恢复光标位置，使用 QTimer 延迟执行
                from PyQt5.QtCore import QTimer
                QTimer.singleShot(50, lambda: self._restore_cursor_position(self.current_tab_id, saved_row))

                # 设置焦点到当前标签页的列表控件
                QTimer.singleShot(60, lambda: self._set_focus_to_current_tab())
        except (AttributeError, TypeError):
            pass

    def _set_focus_to_current_tab(self):
        """设置焦点到当前标签页的列表控件"""
        if self.current_tab_id is None:
            return
        list_widget = self.get_list_widget_for_tab(self.current_tab_id)
        if list_widget:
            list_widget.setFocus()
            # 如果没有选中项，默认选中第一项
            if list_widget.currentRow() < 0 and list_widget.count() > 0:
                list_widget.setCurrentRow(0)

    def _restore_cursor_position(self, tab_id: int, row: int):
        """恢复指定标签页的光标位置"""
        if tab_id != self.current_tab_id:
            return  # 标签页已经改变，不恢复
        list_widget = self.get_list_widget_for_tab(tab_id)
        if list_widget and row >= 0 and row < list_widget.count():
            list_widget.setCurrentRow(row)

    def on_tab_moved(self, from_index: int, to_index: int):
        """标签页拖动排序 - 确保默认页始终在最前"""
        # 获取默认标签页的索引
        default_index = -1
        default_tab_id = None
        for i in range(self.tab_widget.count()):
            try:
                tab_id = self.get_tab_data(i)
                tab = next((t for t in self.tabs if t['id'] == tab_id), None)
                if tab and tab['is_default']:
                    default_index = i
                    default_tab_id = tab_id
                    break
            except (AttributeError, TypeError):
                pass

        # 如果默认页不在位置 0，把它移回去
        if default_index > 0:
            self.tab_widget.tabBar().moveTab(default_index, 0)
            # 重新获取索引
            default_index = 0

        # 获取所有标签页的新顺序（跳过默认页）
        tab_orders = []
        order = 0
        for i in range(self.tab_widget.count()):
            try:
                tab_id = self.get_tab_data(i)
                tab = next((t for t in self.tabs if t['id'] == tab_id), None)
                if tab and not tab['is_default']:
                    tab_orders.append((tab_id, order))
                    order += 1
            except (AttributeError, TypeError):
                pass

        if hasattr(self, 'reorder_tabs_requested') and tab_orders:
            self.reorder_tabs_requested.emit(tab_orders)

    def on_tab_bar_context_menu(self, position):
        """标签栏右键菜单"""
        # 如果已有菜单打开，先关闭它
        if self._tab_menu is not None:
            self._tab_menu.close()
            self._tab_menu = None
            return

        # 获取点击位置的标签索引
        tab_bar = self.tab_widget.tabBar()
        index = tab_bar.tabAt(position)

        menu = QMenu(self)
        self._tab_menu = menu
        font_size = self.config.getint('UI', 'font_size', 12)
        menu.setFont(QFont('Noto Sans CJK SC', font_size))

        # 新建标签页选项
        new_tab_action = QAction('新建标签页', self)
        new_tab_action.triggered.connect(self.on_add_tab_clicked)
        menu.addAction(new_tab_action)

        # 如果有标签页被点击，添加更多选项
        if index >= 0:
            tab_id = self.get_tab_data(index)
            tab = next((t for t in self.tabs if t['id'] == tab_id), None)

            if tab:
                menu.addSeparator()

                # 重命名选项（仅自定义标签页）
                if not tab['is_default']:
                    rename_action = QAction('重命名', self)
                    rename_action.triggered.connect(lambda: self.on_rename_tab_clicked(tab_id, tab['name']))
                    menu.addAction(rename_action)

                    delete_action = QAction('删除', self)
                    delete_action.triggered.connect(lambda: self.on_delete_tab_clicked(tab_id))
                    menu.addAction(delete_action)

        # 菜单关闭时清理引用
        menu.aboutToHide.connect(lambda: setattr(self, '_tab_menu', None))

        # 使用 exec_ 显示菜单，它会自动处理点击外部关闭
        menu.exec_(tab_bar.mapToGlobal(position))

    def on_rename_tab_clicked(self, tab_id: int, current_name: str):
        """重命名标签页"""
        dialog = QInputDialog(self)
        dialog.setWindowTitle('重命名标签页')
        dialog.setLabelText('请输入新名称 (最多10个字符):')
        dialog.setTextValue(current_name)
        dialog.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint | Qt.Tool)
        # 限制输入长度
        from PyQt5.QtWidgets import QLineEdit
        line_edit = dialog.findChild(QLineEdit)
        if line_edit:
            line_edit.setMaxLength(10)
        if dialog.exec_() == QInputDialog.Accepted:
            new_name = dialog.textValue().strip()
            if new_name and len(new_name) <= 10:
                self.rename_tab_requested.emit(tab_id, new_name)
        # 恢复焦点到列表控件
        self._set_focus_to_current_tab()

    def on_delete_tab_clicked(self, tab_id: int):
        """删除标签页"""
        # 确认删除
        confirmed = self._show_confirm_dialog(
            '确认删除',
            '确定要删除该标签页吗？其中的记录将回到默认页。'
        )
        # 恢复焦点到列表控件（无论是否确认）
        self._set_focus_to_current_tab()
        if confirmed:
            self.delete_tab_requested.emit(tab_id)

    def on_add_tab_clicked(self):
        """添加新标签页"""
        # 检查自定义标签页数量上限
        custom_tabs = [t for t in self.tabs if not t['is_default']]
        if len(custom_tabs) >= 3:
            self._show_info_dialog('达到上限', '最多只能创建3个自定义标签页。')
            return

        # 创建置顶的输入对话框
        dialog = QInputDialog(self)
        dialog.setWindowTitle('新建标签页')
        dialog.setLabelText('请输入标签页名称 (最多10个字符):')
        dialog.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint | Qt.Tool)
        # 限制输入长度
        from PyQt5.QtWidgets import QLineEdit
        line_edit = dialog.findChild(QLineEdit)
        if line_edit:
            line_edit.setMaxLength(10)
        if dialog.exec_() == QInputDialog.Accepted:
            name = dialog.textValue().strip()
            if name and len(name) <= 10:
                self.create_tab_requested.emit(name)
        # 恢复焦点到列表控件
        self._set_focus_to_current_tab()

    def on_item_double_clicked(self, item: QListWidgetItem, tab_id: int):
        """项目双击事件 - 粘贴原格式"""
        record_id = item.data(Qt.UserRole)
        self.paste_requested.emit(record_id, False)

    def update_tabs(self, tabs: List[Dict]):
        """更新标签页显示"""
        self.tabs = tabs

        # 保存当前选中的标签页
        current_tab_id = self.current_tab_id

        # 清除现有标签页（保留内容以便复用）
        existing_tabs = {}
        for i in range(self.tab_widget.count()):
            tab_id = self.get_tab_data(i)
            if tab_id is not None:
                existing_tabs[tab_id] = self.tab_widget.widget(i)

        self.tab_widget.clear()

        # 按顺序添加标签页（默认页始终在最前）
        default_tab = next((t for t in tabs if t['is_default']), None)
        custom_tabs = sorted([t for t in tabs if not t['is_default']], key=lambda x: x['sort_order'])
        sorted_tabs = ([default_tab] if default_tab else []) + custom_tabs

        for tab in sorted_tabs:
            tab_id = tab['id']
            tab_name = tab['name']

            # 复用或创建列表控件
            if tab_id in existing_tabs:
                list_widget = existing_tabs[tab_id]
            else:
                list_widget = self.create_list_widget_for_tab(tab_id)

            index = self.tab_widget.addTab(list_widget, tab_name)
            try:
                self.tab_widget.tabBar().setTabData(index, tab_id)
            except (AttributeError, TypeError):
                pass

            # 所有标签页都通过右键菜单管理，隐藏关闭按钮
            try:
                self.tab_widget.tabBar().setTabButton(index, QTabBar.RightSide, None)
            except (AttributeError, TypeError):
                pass

        # 恢复选中的标签页
        if current_tab_id:
            for i in range(self.tab_widget.count()):
                if self.get_tab_data(i) == current_tab_id:
                    self.tab_widget.setCurrentIndex(i)
                    break

        # 更新标签宽度（延迟执行确保布局完成）
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(10, self._update_tab_widths)

    def _update_tab_widths(self):
        """根据标签数量动态调整每个标签的宽度，使其均匀分布占满顶栏"""
        tab_count = self.tab_widget.count()
        if tab_count == 0:
            return

        # 获取标签栏宽度（使用 tabWidget 的宽度，因为 tabBar 的宽度可能不准确）
        # 减去一些边距确保不会超出
        available_width = self.tab_widget.width() - 8

        # 计算每个标签的宽度（均匀分布）
        # 减2px防止舍入误差导致总宽度超出
        tab_width = max((available_width // tab_count) - 2, 60)

        # 获取当前字体大小
        font_size = self.config.getint('UI', 'font_size', 12)

        # 应用样式表 - 为不同标签页设置不同背景色
        self.tab_widget.setStyleSheet(f'''
            QTabWidget::pane {{
                border: none;
                background: transparent;
            }}
            /* 默认基础样式 */
            QTabBar::tab {{
                border-radius: 4px 4px 0 0;
                padding: 6px 2px;
                margin: 0px 1px 0px 0px;
                font-size: {font_size}px;
                color: #333;
                min-width: 60px;
                width: {tab_width}px;
                background: rgba(220, 220, 220, 200);
            }}
            /* 第一个标签页（默认页）- 浅蓝灰 */
            QTabBar::tab:first:!selected {{
                background: rgba(200, 210, 230, 220);
            }}
            /* 最后一个标签页（自定义页3或最后一个）- 浅粉灰 */
            QTabBar::tab:last:!selected {{
                background: rgba(230, 210, 210, 220);
            }}
            /* 中间标签页 - 根据位置使用不同色调 */
            /* 使用 margin 或 right 属性来区分第二个和第三个 */
            QTabBar::tab:middle:!selected {{
                background: rgba(210, 230, 210, 220);
            }}
            /* 选中状态 - 统一蓝色 */
            QTabBar::tab:selected {{
                background: rgba(0, 120, 215, 220);
                color: white;
            }}
            /* 悬停状态 - 加深颜色 */
            QTabBar::tab:first:hover:!selected {{
                background: rgba(185, 195, 215, 240);
            }}
            QTabBar::tab:last:hover:!selected {{
                background: rgba(215, 195, 195, 240);
            }}
            QTabBar::tab:middle:hover:!selected {{
                background: rgba(195, 215, 195, 240);
            }}
            QTabBar::tab:hover:!selected {{
                background: rgba(200, 200, 200, 240);
            }}
            /* 关闭按钮 */
            QTabBar::close-button {{
                image: none;
                width: 14px;
                height: 14px;
                margin-left: 4px;
            }}
            QTabBar::close-button:hover {{
                background: rgba(255, 100, 100, 180);
                border-radius: 2px;
            }}
        ''')

    def resizeEvent(self, event):
        """窗口大小变化时更新标签宽度（防抖处理）"""
        super().resizeEvent(event)
        # 重启定时器，确保停止调整后最终一定会执行更新
        self._resize_timer.stop()
        self._resize_timer.start(100)

    def update_tab_records(self, tab_id: int, records: List[Dict]):
        """更新指定标签页的记录"""
        self.current_records[tab_id] = records

        # 找到对应的列表控件
        list_widget = self.get_list_widget_for_tab(tab_id)
        if list_widget is None:
            return

        list_widget.clear()

        font_size = self.config.getint('UI', 'font_size', 12)
        time_font_size = max(8, font_size - 2)
        text_font_size = font_size + 1  # 记录文字增大1号

        # 判断当前标签页是否为默认页
        is_default_tab = False
        for tab in self.tabs:
            if tab['id'] == tab_id and tab['is_default']:
                is_default_tab = True
                break

        for record in records:
            text = record['plain_text'].replace('\n', ' ').strip()
            if len(text) > 50:
                text = text[:50] + '...'

            item = QListWidgetItem()
            item.setData(Qt.UserRole, record['id'])

            # 设置 HTML 格式的 tooltip，支持自动换行
            item.setToolTip(self._format_tooltip(record['plain_text']))

            # 使用富文本 - 时间字号不变，记录文字增大1号
            # 只有默认页显示时间，自定义标签页不显示时间
            if is_default_tab:
                time_str = record['display_time']
                # 被固定到自定义标签页的记录显示为蓝色
                text_color = '#0066CC' if record.get('is_pinned') else '#000000'
                html_text = f'<span style="font-size:{time_font_size}px; color:#888888;">[{time_str}]</span> <span style="font-size:{text_font_size}px; color:{text_color};">{text}</span>'
            else:
                text_color = '#000000'
                html_text = f'<span style="font-size:{text_font_size}px; color:{text_color};">{text}</span>'

            label = QLabel(html_text)
            label.setWordWrap(False)
            label.setStyleSheet("background: transparent; padding: 4px;")
            label.setAttribute(Qt.WA_TransparentForMouseEvents)

            # 存储显示信息到item，用于后续更新颜色
            item.setData(Qt.UserRole + 1, {
                'label': label,
                'text': text,
                'is_default': is_default_tab,
                'time_font_size': time_font_size,
                'text_font_size': text_font_size,
                'text_color': text_color,
                'display_time': record.get('display_time', '')
            })

            list_widget.addItem(item)
            list_widget.setItemWidget(item, label)

        # 只连接一次选择状态变化信号（使用唯一标识避免重复连接）
        if not hasattr(list_widget, '_selection_connected'):
            list_widget.itemSelectionChanged.connect(
                lambda lw=list_widget: self._update_list_selection_colors(lw)
            )
            list_widget._selection_connected = True

        # 默认选中第一项
        if list_widget.count() > 0:
            list_widget.setCurrentRow(0)

    def _update_list_selection_colors(self, list_widget):
        """更新列表中所有项的文字颜色（选中时为白色，未选中时为默认颜色）"""
        for i in range(list_widget.count()):
            item = list_widget.item(i)
            if not item:
                continue

            # 获取存储的显示信息
            display_info = item.data(Qt.UserRole + 1)
            if not display_info:
                continue

            label = display_info.get('label')
            if not label:
                continue

            # 检查label是否已被删除
            try:
                label.text()
            except RuntimeError:
                continue

            text = display_info['text']
            is_default = display_info['is_default']
            time_font_size = display_info['time_font_size']
            text_font_size = display_info['text_font_size']
            default_text_color = display_info['text_color']
            display_time = display_info['display_time']

            is_selected = item.isSelected()

            if is_selected:
                # 选中时使用白色文字
                if is_default:
                    html_text = f'<span style="font-size:{time_font_size}px; color:#FFFFFF;">[{display_time}]</span> <span style="font-size:{text_font_size}px; color:#FFFFFF;">{text}</span>'
                else:
                    html_text = f'<span style="font-size:{text_font_size}px; color:#FFFFFF;">{text}</span>'
            else:
                # 未选中时使用默认颜色
                if is_default:
                    html_text = f'<span style="font-size:{time_font_size}px; color:#888888;">[{display_time}]</span> <span style="font-size:{text_font_size}px; color:{default_text_color};">{text}</span>'
                else:
                    html_text = f'<span style="font-size:{text_font_size}px; color:#000000;">{text}</span>'
            label.setText(html_text)

    def _on_bottom_bar_enter(self, event):
        """鼠标进入底部栏时启动滚动"""
        # 检查文字是否超出显示区域
        font_metrics = self.title_label.fontMetrics()
        text_width = font_metrics.horizontalAdvance(self._original_title_text)
        label_width = self.title_label.width()

        if text_width > label_width:
            self._scroll_timer.start(150)  # 每150ms滚动一次

    def _on_bottom_bar_leave(self, event):
        """鼠标离开底部栏时停止滚动并重置"""
        self._scroll_timer.stop()
        self._scroll_pos = 0
        self.title_label.setText(self._original_title_text)

    def _scroll_title_text(self):
        """滚动标题文字"""
        text = self._original_title_text
        if len(text) <= 1:
            return

        # 循环滚动效果
        self._scroll_pos = (self._scroll_pos + 1) % len(text)
        scrolled_text = text[self._scroll_pos:] + "  |  " + text[:self._scroll_pos]
        self.title_label.setText(scrolled_text)

    def keyPressEvent(self, event: QKeyEvent):
        """键盘事件处理"""
        key = event.key()
        modifiers = event.modifiers()

        if key == Qt.Key_Escape:
            self.hide()
            self.closed.emit()
            return

        # 左右键切换标签页（不需要list_widget）
        if key == Qt.Key_Left:
            # 左键：切换到上一个标签页
            current_index = self.tab_widget.currentIndex()
            if current_index > 0:
                self.tab_widget.setCurrentIndex(current_index - 1)
            return

        elif key == Qt.Key_Right:
            # 右键：切换到下一个标签页
            current_index = self.tab_widget.currentIndex()
            if current_index < self.tab_widget.count() - 1:
                self.tab_widget.setCurrentIndex(current_index + 1)
            return

        # 获取当前标签页的列表控件
        list_widget = self.get_list_widget_for_tab(self.current_tab_id)
        if not list_widget:
            return

        current_item = list_widget.currentItem()

        if key == Qt.Key_Up:
            current_row = list_widget.currentRow()
            if current_row > 0:
                list_widget.setCurrentRow(current_row - 1)

        elif key == Qt.Key_Down:
            current_row = list_widget.currentRow()
            if current_row < list_widget.count() - 1:
                list_widget.setCurrentRow(current_row + 1)

        elif key in (Qt.Key_Return, Qt.Key_Enter):
            if current_item:
                # 检测是否多选
                selected_items = list_widget.selectedItems()
                if len(selected_items) > 1:
                    self._show_info_dialog('提示', '多选状态下无法粘贴，请单选一项后再粘贴')
                    return
                record_id = current_item.data(Qt.UserRole)
                is_plain = (modifiers == Qt.ShiftModifier)
                self.paste_requested.emit(record_id, is_plain)

        elif key == Qt.Key_1 and modifiers == Qt.ControlModifier:
            # Ctrl+1: 粘贴纯文本（与 Shift+Enter 逻辑统一）
            if current_item:
                # 检测是否多选
                selected_items = list_widget.selectedItems()
                if len(selected_items) > 1:
                    self._show_info_dialog('提示', '多选状态下无法粘贴，请单选一项后再粘贴')
                    return
                record_id = current_item.data(Qt.UserRole)
                self.paste_requested.emit(record_id, True)

        elif key == Qt.Key_Home:
            if list_widget.count() > 0:
                list_widget.setCurrentRow(0)

        elif key == Qt.Key_End:
            last_idx = list_widget.count() - 1
            if last_idx >= 0:
                list_widget.setCurrentRow(last_idx)

        elif key == Qt.Key_PageUp:
            current_row = list_widget.currentRow()
            new_row = max(0, current_row - 5)
            list_widget.setCurrentRow(new_row)

        elif key == Qt.Key_PageDown:
            current_row = list_widget.currentRow()
            new_row = min(list_widget.count() - 1, current_row + 5)
            list_widget.setCurrentRow(new_row)

        elif key == Qt.Key_Delete:
            if not current_item:
                return

            # 获取当前标签页信息
            current_tab = next((t for t in self.tabs if t['id'] == self.current_tab_id), None)
            if not current_tab:
                return

            # 检测是否多选（复选）
            selected_items = list_widget.selectedItems()
            if len(selected_items) > 1:
                # 复选时：弹出确认对话框确认删除/移除
                record_ids = [item.data(Qt.UserRole) for item in selected_items]
                if current_tab['is_default']:
                    # 默认页：确认批量删除
                    self._confirm_batch_delete(record_ids)
                else:
                    # 自定义页：确认批量取消固定
                    count = len(record_ids)
                    if self._show_confirm_dialog(
                        '确认批量取消固定',
                        f'确定要取消固定选中的 {count} 条记录吗？\n取消后记录将回到默认页。'
                    ):
                        for record_id in record_ids:
                            self.unpin_record_requested.emit(record_id, self.current_tab_id)
            else:
                # 单选时
                record_id = current_item.data(Qt.UserRole)
                if current_tab['is_default']:
                    # 默认标签页单选：直接删除记录（无确认）
                    self.delete_record_requested.emit(record_id)
                else:
                    # 自定义标签页单选：弹出确认对话框取消固定
                    if self._show_confirm_dialog(
                        '确认取消固定',
                        '确定要取消固定此记录吗？\n取消后记录将回到默认页。'
                    ):
                        self.unpin_record_requested.emit(record_id, self.current_tab_id)

    def _format_tooltip(self, text: str) -> str:
        """格式化 tooltip 文本为 HTML，支持自动换行"""
        # 限制长度防止过长
        tooltip_text = text[:500] if len(text) > 500 else text
        # HTML 转义
        tooltip_html = tooltip_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        # 保留换行符
        tooltip_html = tooltip_html.replace('\n', '<br>')
        # 计算最大宽度（窗口宽度的85%，最小300，最大600）
        window_width = self.width() if self.width() > 0 else 400
        max_width = max(300, min(int(window_width * 0.85), 600))
        # 返回 HTML 格式的 tooltip
        return f'<div style="max-width: {max_width}px; word-wrap: break-word; white-space: pre-wrap;">{tooltip_html}</div>'

    def _show_confirm_dialog(self, title: str, text: str) -> bool:
        """显示确认对话框，居中于主窗口，置于顶层"""
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(title)
        msg_box.setText(text)
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setDefaultButton(QMessageBox.No)
        msg_box.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        # 对话框居中于主窗口
        if self.isVisible():
            geo = msg_box.geometry()
            geo.moveCenter(self.geometry().center())
            msg_box.setGeometry(geo)

        return msg_box.exec_() == QMessageBox.Yes

    def _show_info_dialog(self, title: str, text: str):
        """显示信息提示对话框，居中于主窗口，置于顶层"""
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(title)
        msg_box.setText(text)
        msg_box.setStandardButtons(QMessageBox.Ok)
        msg_box.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        # 对话框居中于主窗口
        if self.isVisible():
            geo = msg_box.geometry()
            geo.moveCenter(self.geometry().center())
            msg_box.setGeometry(geo)

        msg_box.exec_()

    def _confirm_unpin(self, record_id: int, tab_id: int):
        """确认后取消固定记录"""
        if self._show_confirm_dialog(
            '确认取消固定',
            '确定要从当前标签页移除此记录吗？\n移除后记录将回到默认页。'
        ):
            self.unpin_record_requested.emit(record_id, tab_id)

    def show_at_cursor(self):
        """在鼠标位置显示窗口"""
        cursor_pos = QCursor.pos()
        x = cursor_pos.x() - self.width() // 2
        y = cursor_pos.y() + 25

        screen = QApplication.primaryScreen().geometry()
        x = max(0, min(x, screen.width() - self.width()))
        y = max(0, min(y, screen.height() - self.height()))

        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()

        # 加载标签页
        self.load_tabs_requested.emit()

    def hideEvent(self, event):
        """窗口隐藏事件 - 保存窗口大小"""
        # 保存窗口大小到配置
        size = self.size()
        self.config.setint('UI', 'window_width', size.width())
        self.config.setint('UI', 'window_height', size.height())
        self.config.save_config()

        # 关闭标签页右键菜单
        if self._tab_menu and self._tab_menu.isVisible():
            self._tab_menu.close()
            self._tab_menu = None
        self.closed.emit()
        super().hideEvent(event)

    def eventFilter(self, obj, event):
        """事件过滤器：检测窗口失焦、点击菜单外部、列表Delete键"""
        # 处理列表控件的Delete键
        if isinstance(obj, QListWidget) and event.type() == event.KeyPress:
            key = event.key()
            if key == Qt.Key_Delete:
                current_item = obj.currentItem()
                if current_item:
                    # 获取该列表控件所属的标签页ID
                    for tab in self.tabs:
                        list_widget = self.get_list_widget_for_tab(tab['id'])
                        if list_widget == obj:
                            # 检测是否多选（复选）
                            selected_items = obj.selectedItems()
                            if len(selected_items) > 1:
                                # 复选时：弹出确认对话框确认删除/移除
                                record_ids = [item.data(Qt.UserRole) for item in selected_items]
                                if tab['is_default']:
                                    # 默认页：确认批量删除
                                    self._confirm_batch_delete(record_ids)
                                else:
                                    # 自定义页：确认批量取消固定
                                    count = len(record_ids)
                                    if self._show_confirm_dialog(
                                        '确认批量取消固定',
                                        f'确定要取消固定选中的 {count} 条记录吗？\n取消后记录将回到默认页。'
                                    ):
                                        for record_id in record_ids:
                                            self.unpin_record_requested.emit(record_id, tab['id'])
                            else:
                                # 单选时
                                record_id = current_item.data(Qt.UserRole)
                                if tab['is_default']:
                                    # 默认标签页单选：直接删除记录（无确认）
                                    self.delete_record_requested.emit(record_id)
                                else:
                                    # 自定义标签页单选：弹出确认对话框取消固定
                                    if self._show_confirm_dialog(
                                        '确认取消固定',
                                        '确定要取消固定此记录吗？\n取消后记录将回到默认页。'
                                    ):
                                        self.unpin_record_requested.emit(record_id, tab['id'])
                            return True
        if obj == self:
            # 鼠标按下时检查是否点击了菜单外部
            if event.type() == event.MouseButtonPress:
                if self._tab_menu and self._tab_menu.isVisible():
                    # 检查点击位置是否在菜单外部
                    if not self._tab_menu.geometry().contains(event.globalPos()):
                        self._tab_menu.close()
                        self._tab_menu = None

            if event.type() == event.WindowDeactivate:
                # 关闭标签页右键菜单
                if self._tab_menu and self._tab_menu.isVisible():
                    self._tab_menu.close()
                    self._tab_menu = None

                # 检查是否有模态对话框正在显示
                from PyQt5.QtWidgets import QApplication
                active_window = QApplication.activeWindow()
                if active_window and active_window != self:
                    # 有模态对话框正在显示，不隐藏窗口
                    return False
                if self.isVisible():
                    self.hide()
                    self.closed.emit()
                return False
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event):
        """鼠标按下事件 - 检测是否在调整大小区域"""
        if event.button() == Qt.LeftButton:
            self._resize_edge = self._get_resize_edge(event.pos())
            if self._resize_edge:
                self._resize_start_pos = event.globalPos()
                self._resize_start_geometry = self.geometry()
                event.accept()
            else:
                # 允许拖动窗口
                self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
                event.accept()

    def mouseMoveEvent(self, event):
        """鼠标移动事件 - 调整光标形状或调整大小"""
        if event.buttons() == Qt.NoButton:
            # 仅移动鼠标，更新光标形状
            edge = self._get_resize_edge(event.pos())
            self._set_cursor_for_edge(edge)
        elif event.buttons() == Qt.LeftButton and self._resize_edge:
            # 正在调整大小
            self._perform_resize(event.globalPos())
            event.accept()
        elif event.buttons() == Qt.LeftButton and hasattr(self, '_drag_pos'):
            # 拖动窗口
            self.move(event.globalPos() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        """鼠标释放事件 - 结束调整大小"""
        if event.button() == Qt.LeftButton:
            self._resize_edge = None
            self._resize_start_pos = None
            self._resize_start_geometry = None
            if hasattr(self, '_drag_pos'):
                delattr(self, '_drag_pos')
            self.unsetCursor()
            event.accept()

    def _get_resize_edge(self, pos):
        """检测鼠标位置是否在调整大小边缘"""
        rect = self.rect()
        margin = self._resize_margin

        # 检测右下角
        if pos.x() >= rect.width() - margin and pos.y() >= rect.height() - margin:
            return 'bottom_right'
        # 检测右侧
        elif pos.x() >= rect.width() - margin:
            return 'right'
        # 检测下侧
        elif pos.y() >= rect.height() - margin:
            return 'bottom'
        return None

    def _set_cursor_for_edge(self, edge):
        """根据边缘设置光标形状"""
        if edge == 'bottom_right':
            self.setCursor(Qt.SizeFDiagCursor)
        elif edge in ('right', 'bottom'):
            self.setCursor(Qt.SizeFDiagCursor if edge == 'bottom_right' else Qt.SizeHorCursor if edge == 'right' else Qt.SizeVerCursor)
        else:
            self.unsetCursor()

    def _perform_resize(self, global_pos):
        """执行调整大小"""
        if not self._resize_start_geometry or not self._resize_start_pos:
            return

        delta = global_pos - self._resize_start_pos
        new_geometry = QRect(self._resize_start_geometry)

        if self._resize_edge in ('right', 'bottom_right'):
            new_geometry.setWidth(self._resize_start_geometry.width() + delta.x())
        if self._resize_edge in ('bottom', 'bottom_right'):
            new_geometry.setHeight(self._resize_start_geometry.height() + delta.y())

        # 确保在最小和最大尺寸范围内
        min_w, min_h = self.minimumSize().width(), self.minimumSize().height()
        max_w, max_h = self.maximumSize().width(), self.maximumSize().height()

        new_width = max(min_w, min(new_geometry.width(), max_w))
        new_height = max(min_h, min(new_geometry.height(), max_h))

        new_geometry.setWidth(new_width)
        new_geometry.setHeight(new_height)

        self.setGeometry(new_geometry)


# ==================== 全局热键管理器 ====================
class GlobalHotkeyManager(QObject):
    """全局热键管理器"""

    show_triggered = pyqtSignal()  # 显示/隐藏窗口信号

    def __init__(self, config: ConfigManager, parent=None):
        super().__init__(parent)
        self.config = config
        self.listener = None
        self.hotkey_manager = None
        self.running = False

    def start(self):
        """启动热键监听"""
        self.running = True

        if PYNPUT_AVAILABLE:
            try:
                self._start_pynput()
                return
            except (ImportError, OSError, AttributeError) as e:
                logger.warning("pynput 启动失败: %s", e)
                logger.info("尝试使用 system_hotkey...")

        if SYSTEM_HOTKEY_AVAILABLE:
            try:
                self._start_system_hotkey()
                return
            except (ImportError, OSError, AttributeError) as e:
                logger.warning("system_hotkey 启动失败: %s", e)

        logger.error("没有可用的全局热键库，请安装 pynput 或 system_hotkey")

    def _start_pynput(self):
        """使用 pynput 监听热键 - 使用 Listener 方式更可靠"""
        from pynput.keyboard import Key, Listener

        # 跟踪 Ctrl 键状态
        self._ctrl_pressed = False

        def on_press(key):
            try:
                # 检测 Ctrl 键
                if key == Key.ctrl_l or key == Key.ctrl_r:
                    self._ctrl_pressed = True
                    return

                # 检测 Ctrl+~ (反引号键)
                if self._ctrl_pressed:
                    # 方法1: 检测 vk 码 (反引号键 vk=41 或 96 取决于系统)
                    if hasattr(key, 'vk') and key.vk in (41, 96, 192):
                        self.show_triggered.emit()
                        return
                    # 方法2: 检测字符形式的 `
                    if hasattr(key, 'char') and key.char == '`':
                        self.show_triggered.emit()
                        return
                    # 方法3: 检测 Key.grave (如果存在) - 安全访问
                    try:
                        if key == Key.grave:
                            self.show_triggered.emit()
                            return
                    except AttributeError:
                        pass  # Key.grave 不存在，忽略
            except (AttributeError, TypeError):
                pass  # Key.grave 不存在，忽略
            except (AttributeError, TypeError, ValueError) as e:
                # 忽略常见的热键检测噪声错误
                if "grave" not in str(e).lower():
                    logger.warning("热键检测错误: %s", e)

        def on_release(key):
            if key == Key.ctrl_l or key == Key.ctrl_r:
                self._ctrl_pressed = False

        self.listener = Listener(on_press=on_press, on_release=on_release)
        self.listener.start()
        logger.info("全局热键已注册: Ctrl+~ 显示/隐藏剪贴板窗口")

    def _start_system_hotkey(self):
        """使用 system_hotkey 监听热键"""
        try:
            self.hotkey_manager = SystemHotkey()
            # 尝试注册 Ctrl+~ (使用 backtick 作为备选)
            try:
                self.hotkey_manager.register(('ctrl', 'grave'), callback=self.show_triggered.emit)
                logger.info("全局热键已注册: Ctrl+~ 显示/隐藏剪贴板窗口")
            except (KeyError, ValueError, OSError):
                try:
                    self.hotkey_manager.register(('ctrl', '`'), callback=self.show_triggered.emit)
                    logger.info("全局热键已注册: Ctrl+` 显示/隐藏剪贴板窗口")
                except (KeyError, ValueError, OSError) as e2:
                    logger.error("system_hotkey 注册失败: %s", e2)
        except (ImportError, OSError, AttributeError) as e:
            logger.error("system_hotkey 初始化失败: %s", e)

    def stop(self):
        """停止热键监听"""
        self.running = False

        if self.listener:
            try:
                self.listener.stop()
            except (RuntimeError, OSError) as e:
                logger.warning("停止 pynput 监听失败: %s", e)

        if self.hotkey_manager:
            try:
                self.hotkey_manager.unregister(('ctrl', 'grave'))
            except (KeyError, ValueError, OSError):
                try:
                    self.hotkey_manager.unregister(('ctrl', '`'))
                except (KeyError, ValueError, OSError) as e2:
                    logger.warning("停止 system_hotkey 失败: %s", e2)


# ==================== 键盘模拟器 ====================
class KeyboardSimulator(QObject):
    """键盘模拟器，用于发送粘贴命令 - 使用异步 QProcess 避免阻塞主线程

    使用纯异步回调链实现，完全避免 QEventLoop 阻塞模式
    """

    _instance = None
    _xdotool_process = None
    _timer = None
    _finished_callback = None

    # 信号定义
    paste_completed = pyqtSignal(bool)  # 粘贴完成信号，参数表示成功/失败

    def __init__(self):
        super().__init__()
        self._logger = logging.getLogger('copyU.keyboard')
        self._init_process()

    @classmethod
    def get_instance(cls):
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = KeyboardSimulator()
        return cls._instance

    def _init_process(self):
        """初始化 QProcess"""
        if self._xdotool_process is None:
            self._xdotool_process = QProcess(self)
            self._xdotool_process.setProgram("/usr/bin/xdotool")
            self._xdotool_process.finished.connect(self._on_process_finished)

    def _on_process_finished(self, exit_code, exit_status):
        """QProcess 完成回调"""
        if self._timer:
            self._timer.stop()
            self._timer = None

        callback = self._finished_callback
        self._finished_callback = None

        if callback:
            success = (exit_code == 0 and exit_status == QProcess.NormalExit)
            if not success and exit_code != 0:
                stderr = self._xdotool_process.readAllStandardError().data().decode('utf-8', errors='ignore')
                if stderr:
                    self._logger.warning("xdotool 执行失败: %s", stderr)
            callback(success)

    def run_xdotool_async(self, args: list, callback, timeout_ms: int = 500):
        """异步运行 xdotool 命令

        Args:
            args: xdotool 参数列表
            callback: 完成回调函数，接收一个 bool 参数表示成功/失败
            timeout_ms: 超时时间（毫秒）
        """
        import shutil

        if not shutil.which("xdotool"):
            self._logger.warning("xdotool 未安装")
            callback(False)
            return

        try:
            # 确保进程不在运行中 - 使用异步方式终止
            if self._xdotool_process.state() != QProcess.NotRunning:
                self._xdotool_process.kill()
                # 异步等待，非阻塞，使用 QTimer 延迟启动新命令
                def delayed_start():
                    self._do_run_async(args, callback, timeout_ms)
                QTimer.singleShot(50, delayed_start)
            else:
                self._do_run_async(args, callback, timeout_ms)

        except (OSError, IOError, RuntimeError) as e:
            self._logger.warning("QProcess 执行失败: %s", e)
            self._finished_callback = None
            callback(False)

    def _do_run_async(self, args: list, callback, timeout_ms: int):
        """实际执行异步命令（内部方法）"""
        self._finished_callback = callback
        self._xdotool_process.setArguments(args)

        # 设置超时定时器
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._on_timeout(args))
        self._timer.start(timeout_ms)

        self._xdotool_process.start()

    def _on_timeout(self, args):
        """处理超时"""
        self._logger.warning("xdotool 执行超时: %s", args)
        try:
            self._xdotool_process.kill()
        except Exception:
            pass

        callback = self._finished_callback
        self._finished_callback = None
        if callback:
            callback(False)

    @classmethod
    def simulate_paste_async(cls, callback=None):
        """模拟 Ctrl+V 粘贴 - 完全异步非阻塞执行

        Args:
            callback: 可选的回调函数，接收 bool 参数表示成功/失败
        """
        import shutil
        import logging

        logger = logging.getLogger('copyU.keyboard')
        simulator = cls.get_instance()

        def on_paste_done(success):
            """粘贴完成的统一回调"""
            if callback:
                callback(success)
            simulator.paste_completed.emit(success)

        # 优先使用 xdotool（Linux X11 环境）
        if shutil.which("xdotool"):
            # 使用 QTimer 延迟启动，确保用户已释放按键
            def start_keyup():
                def on_keyup_done(success):
                    if success:
                        # keyup 成功，继续执行 key
                        def do_key():
                            simulator.run_xdotool_async(
                                ["key", "ctrl+v"],
                                on_key_done,
                                timeout_ms=300
                            )

                        def on_key_done(success):
                            if success:
                                logger.debug("使用 xdotool 模拟粘贴成功")
                                on_paste_done(True)
                            else:
                                logger.warning("xdotool key 命令失败，尝试 pyautogui")
                                cls._fallback_to_pyautogui(on_paste_done)

                        QTimer.singleShot(20, do_key)  # 20ms 延迟
                    else:
                        logger.warning("xdotool keyup 命令失败，尝试 pyautogui")
                        cls._fallback_to_pyautogui(on_paste_done)

                simulator.run_xdotool_async(
                    ["keyup", "ctrl", "alt", "shift", "meta"],
                    on_keyup_done,
                    timeout_ms=300
                )

            QTimer.singleShot(50, start_keyup)  # 50ms 延迟确保按键释放
        else:
            logger.info("xdotool 未找到，尝试 pyautogui")
            cls._fallback_to_pyautogui(on_paste_done)

    @classmethod
    def _fallback_to_pyautogui(cls, callback):
        """回退到 pyautogui 实现 - 在后台线程中执行避免阻塞"""
        import logging
        logger = logging.getLogger('copyU.keyboard')

        def run_pyautogui():
            try:
                import pyautogui
                import time
                pyautogui.PAUSE = 0.01
                pyautogui.keyUp('ctrl')
                pyautogui.keyUp('v')
                pyautogui.keyUp('alt')
                pyautogui.keyUp('shift')
                time.sleep(0.05)
                pyautogui.keyDown('ctrl')
                pyautogui.keyDown('v')
                pyautogui.keyUp('v')
                pyautogui.keyUp('ctrl')
                logger.debug("使用 pyautogui 模拟粘贴成功")
                return True
            except ImportError:
                logger.error("无法模拟键盘，请安装 xdotool 或 pyautogui")
                return False
            except Exception as e:
                logger.error("pyautogui 执行失败: %s", e)
                return False

        # 使用 QTimer 延迟执行，避免阻塞主事件循环
        def delayed_run():
            success = run_pyautogui()
            callback(success)

        QTimer.singleShot(50, delayed_run)

    @staticmethod
    def simulate_paste():
        """模拟 Ctrl+V 粘贴 - 兼容旧接口，内部调用异步版本

        注意：此静态方法已废弃，新代码应使用 simulate_paste_async
        """
        KeyboardSimulator.simulate_paste_async()


# ==================== 系统托盘图标 ====================
class TrayIcon(QSystemTrayIcon):
    """系统托盘图标"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_app = parent
        self.setup_menu()
        self.setup_icon()

    def setup_menu(self):
        """设置右键菜单"""
        self.menu = QMenu()

        # 显示/隐藏窗口
        self.show_action = QAction('显示剪贴板历史', self)
        self.menu.addAction(self.show_action)

        self.menu.addSeparator()

        # 开机启动选项
        self.autostart_action = QAction('开机启动', self)
        self.autostart_action.setCheckable(True)
        self.autostart_action.setChecked(self.is_autostart_enabled())
        self.autostart_action.triggered.connect(self.toggle_autostart)
        self.menu.addAction(self.autostart_action)

        self.menu.addSeparator()

        # 清理记录
        self.cleanup_action = QAction('清理记录', self)
        self.menu.addAction(self.cleanup_action)

        self.menu.addSeparator()

        # 关于
        self.about_action = QAction('关于', self)
        self.about_action.triggered.connect(self.show_about)
        self.menu.addAction(self.about_action)

        self.menu.addSeparator()

        # 退出
        self.quit_action = QAction('退出', self)
        self.quit_action.triggered.connect(QApplication.instance().quit)
        self.menu.addAction(self.quit_action)

        self.setContextMenu(self.menu)

    def show_about(self):
        """显示关于对话框"""
        QMessageBox.about(
            None,
            '关于 CopyU',
            '<h2>CopyU v1.3.2-beta</h2>'
            '<p>基于 PyQt5 + SQLite3 开发的轻量级剪贴板管理器</p>'
            '<p>支持全局热键、历史记录、纯文本/HTML格式粘贴</p>'
            '<hr>'
            '<p><b>开源信息：</b></p>'
            '<p>GitHub: <a href="https://github.com/estnia/copyU">https://github.com/estnia/copyU</a></p>'
            '<p>License: MIT</p>'
            '<hr>'
            '<p><b>作者：</b> estnia</p>'
            '<p>构建日期：2026-03-04</p>'
        )

    def is_autostart_enabled(self) -> bool:
        """检查是否已设置开机启动"""
        autostart_dir = os.path.expanduser('~/.config/autostart')
        desktop_file = os.path.join(autostart_dir, 'copyu.desktop')
        return os.path.exists(desktop_file)

    def toggle_autostart(self, enabled: bool):
        """切换开机启动状态"""
        autostart_dir = os.path.expanduser('~/.config/autostart')
        desktop_file = os.path.join(autostart_dir, 'copyu.desktop')

        if enabled:
            # 创建 autostart 目录
            os.makedirs(autostart_dir, exist_ok=True)

            # 获取当前脚本路径
            script_path = os.path.abspath(sys.argv[0])
            script_dir = os.path.dirname(script_path)
            icon_path = os.path.join(script_dir, 'icon.svg')

            # 创建 .desktop 文件内容
            desktop_content = f"""[Desktop Entry]
Name=copyU
Comment=剪贴板管理器
Exec=python3 {script_path}
Icon={icon_path}
Type=Application
Terminal=false
Categories=Utility;
StartupNotify=false
X-GNOME-Autostart-enabled=true
"""

            try:
                with open(desktop_file, 'w', encoding='utf-8') as f:
                    f.write(desktop_content)
                os.chmod(desktop_file, 0o755)
                logger.info("已启用开机启动: %s", desktop_file)
            except (OSError, IOError, PermissionError) as e:
                logger.error("启用开机启动失败: %s", e)
                self.autostart_action.setChecked(False)
        else:
            # 删除 .desktop 文件
            try:
                if os.path.exists(desktop_file):
                    os.remove(desktop_file)
                    logger.info("已禁用开机启动")
            except (OSError, IOError, PermissionError) as e:
                logger.error("禁用开机启动失败: %s", e)
                self.autostart_action.setChecked(True)

    def setup_icon(self):
        """设置图标 - 使用设计的SVG图标"""
        # 获取脚本所在目录
        script_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(script_dir, 'icon.svg')

        # 尝试加载SVG图标
        if os.path.exists(icon_path):
            self.setIcon(QIcon(icon_path))
        else:
            # 备用：生成简单的蓝色图标
            from PyQt5.QtGui import QPixmap, QPainter, QBrush
            pixmap = QPixmap(32, 32)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setBrush(QBrush(QColor(0, 120, 215)))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(2, 2, 28, 28)
            painter.end()
            self.setIcon(QIcon(pixmap))

        self.setToolTip('copyU - 剪贴板管理器')


# ==================== 主应用程序 ====================
class CopyUApp(QApplication):
    """copyU 主应用程序"""

    def __init__(self, argv):
        super().__init__(argv)

        # 单实例检测 - 使用 QLocalSocket/Server
        self._local_server = None
        if not self._is_first_instance():
            logger.info("copyU 已在运行，退出新实例...")
            sys.exit(0)

        # 设置应用在最后窗口关闭时不自动退出（因为有系统托盘）
        self.setQuitOnLastWindowClosed(False)

        # 初始化配置
        self.config = ConfigManager()

        # 初始化速率限制器 - 防止高频触发导致系统过载
        # 剪贴板：每秒最多20次变更检测（正常复制操作远低于此）
        self.clipboard_limiter = RateLimiter(max_calls=20, window_seconds=1.0)
        # 热键：每秒最多10次触发（防止快速重复按键）
        self.hotkey_limiter = RateLimiter(max_calls=10, window_seconds=1.0)
        # 粘贴窗口切换：每秒最多5次（防止快速闪烁）
        self.window_toggle_limiter = RateLimiter(max_calls=5, window_seconds=1.0)

        # 初始化线程池管理器
        self.thread_pool = ThreadPoolManager(max_threads=4, max_queue_size=500)
        self.db_worker = DatabaseWorker(
            db_path=self.config.get('General', 'database_path', 'clipboard_store.db'),
            max_age_days=self.config.getint('General', 'max_age_days', 3),
            max_size_mb=self.config.getint('General', 'max_record_size_mb', 1)
        )
        self.db_worker.records_loaded.connect(self.on_records_loaded)
        self.db_worker.record_saved.connect(self.on_record_saved)
        self.db_worker.cleanup_done.connect(self.on_cleanup_done)
        self.db_worker.error_occurred.connect(self.on_db_error)
        # 标签页相关信号
        self.db_worker.tabs_loaded.connect(self.on_tabs_loaded)
        self.db_worker.tab_records_loaded.connect(self.on_tab_records_loaded)
        self.db_worker.tab_created.connect(self.on_tab_created)
        self.db_worker.tab_renamed.connect(self.on_tab_renamed)
        self.db_worker.tab_deleted.connect(self.on_tab_deleted)
        self.db_worker.record_pinned.connect(self.on_record_pinned)
        self.db_worker.record_unpinned.connect(self.on_record_unpinned)
        self.db_worker.record_moved.connect(self.on_record_moved)
        self.db_worker.record_deleted.connect(self.on_record_deleted)
        self.db_worker.start()

        # 启动时执行一次清理
        self.db_worker.add_task({'type': 'cleanup'})

        # 初始化剪贴板管理器
        self.clipboard_manager = ClipboardManager(self.clipboard())

        # 初始化粘贴窗口
        self.paste_window = PasteWindow(self.config)
        self.paste_window.paste_requested.connect(self.on_paste_requested)
        self.paste_window.closed.connect(self.on_paste_window_closed)
        # 标签页相关信号
        self.paste_window.load_tabs_requested.connect(self.on_load_tabs_requested)
        self.paste_window.load_tab_records_requested.connect(self.on_load_tab_records_requested)
        self.paste_window.create_tab_requested.connect(self.on_create_tab_requested)
        self.paste_window.rename_tab_requested.connect(self.on_rename_tab_requested)
        self.paste_window.delete_tab_requested.connect(self.on_delete_tab_requested)
        self.paste_window.pin_record_requested.connect(self.on_pin_record_requested)
        self.paste_window.unpin_record_requested.connect(self.on_unpin_record_requested)
        self.paste_window.delete_record_requested.connect(self.on_delete_record_requested)
        self.paste_window.move_record_requested.connect(self.on_move_record_requested)
        self.paste_window.reorder_tabs_requested.connect(self.on_reorder_tabs_requested)
        self.paste_window.reorder_records_requested.connect(self.on_reorder_records_requested)
        # 批量操作信号连接
        self.paste_window.batch_pin_requested.connect(self.on_batch_pin_requested)
        self.paste_window.batch_unpin_requested.connect(self.on_batch_unpin_requested)
        self.paste_window.batch_delete_requested.connect(self.on_batch_delete_requested)

        # 初始化全局热键
        self.hotkey_manager = GlobalHotkeyManager(self.config)
        self.hotkey_manager.show_triggered.connect(self.toggle_paste_window)
        self.hotkey_manager.start()

        # 连接系统剪贴板变化信号（复用系统 Ctrl+C/V）
        self.clipboard().dataChanged.connect(self.on_clipboard_changed)

        # 初始化系统托盘
        self.tray_icon = TrayIcon(self)
        self.tray_icon.show_action.triggered.connect(self.show_paste_window)
        self.tray_icon.cleanup_action.triggered.connect(self.on_clear_all_requested)
        self.tray_icon.show()

        # 设置定时清理
        cleanup_hours = self.config.getint('General', 'cleanup_interval_hours', 1)
        self.cleanup_timer = QTimer()
        self.cleanup_timer.timeout.connect(self.trigger_cleanup)
        self.cleanup_timer.start(cleanup_hours * 3600 * 1000)  # 转换为毫秒

        # 窗口显示标志
        self.paste_window_visible = False

        logger.info("copyU 已启动")
        logger.info("=" * 40)
        logger.info("快捷键说明:")
        logger.info("  Ctrl+~       - 显示/隐藏剪贴板窗口")
        logger.info("  Ctrl+C       - 复制（系统自动保存到默认页）")
        logger.info("  ↑/↓          - 选择历史记录")
        logger.info("  ←/→          - 切换标签页")
        logger.info("  Enter        - 粘贴原格式")
        logger.info("  Shift+Enter  - 粘贴纯文本")
        logger.info("  Ctrl+1       - 粘贴纯文本（备用快捷键）")
        logger.info("  Ctrl+单击    - 多选/取消选择单个项目")
        logger.info("  Shift+单击   - 选择连续范围的项目")
        logger.info("  双击         - 直接粘贴原格式")
        logger.info("  Esc          - 关闭窗口")
        logger.info("")
        logger.info("标签页功能:")
        logger.info("  - 默认页：所有新记录自动保存至此")
        logger.info("  - 自定义标签页：可创建最多3个，可拖放排序")
        logger.info("  - 右键记录：固定到/移动到其他标签页")
        logger.info("  - Ctrl/Shift+多选后右键：批量固定/移除/删除")
        logger.info("  - 自定义标签页内记录可拖动排序")
        logger.info("=" * 40)

    def on_clipboard_changed(self):
        """系统剪贴板变化时自动保存（复用系统 Ctrl+C/V）"""
        # 速率限制检查
        if not self.clipboard_limiter.is_allowed():
            metrics.increment("clipboard_rate_limited")
            return

        # 获取剪贴板内容
        html_content, plain_text = self.clipboard_manager.get_content()

        if not plain_text and not html_content:
            return

        # 大小限制检查 - 防止极大文本导致卡顿
        content_size = len(plain_text) if plain_text else 0
        if content_size > MAX_CLIPBOARD_SIZE:
            logger.warning("剪贴板内容过大 (%s 字符)，已跳过保存", content_size)
            metrics.increment("clipboard_oversized")
            return

        # 检查重复
        if self.clipboard_manager.is_duplicate(plain_text):
            return

        # 记录指标
        metrics.increment("clipboard_changed")

        # 提交保存任务
        self.db_worker.add_task({
            'type': 'save',
            'html_content': html_content,
            'plain_text': plain_text,
            'app_name': ''
        })

    def toggle_paste_window(self):
        """Ctrl+~ 热键处理: 切换显示/隐藏剪贴板窗口"""
        # 速率限制检查 - 防止高频重复触发
        if not self.window_toggle_limiter.is_allowed():
            metrics.increment("window_toggle_rate_limited")
            return

        # 高频调用节流 - 防止 50ms 内重复执行
        now = time.monotonic()
        if hasattr(self, '_last_toggle_time') and now - self._last_toggle_time < 0.05:
            return
        self._last_toggle_time = now

        metrics.increment("window_toggle")

        logger.debug("Ctrl+~ 触发: 切换剪贴板窗口")
        try:
            if self.paste_window_visible:
                logger.debug("隐藏窗口")
                self.paste_window.hide()
                self.paste_window_visible = False
            else:
                logger.debug("显示窗口")
                # 显示窗口并加载标签页
                self.paste_window.show_at_cursor()
                self.paste_window_visible = True
                # 延迟设置焦点到当前标签页的列表
                from PyQt5.QtCore import QTimer
                QTimer.singleShot(100, lambda: self.paste_window._set_focus_to_current_tab())
            logger.debug("切换完成")
        except (OSError, RuntimeError, AttributeError) as e:
            logger.exception("toggle_paste_window 异常: %s", e)

    def show_paste_window(self):
        """显示粘贴窗口（从托盘菜单调用）"""
        self.toggle_paste_window()

    def on_records_loaded(self, records: List[Dict]):
        """记录加载完成（兼容旧版信号，更新默认标签页）"""
        # 获取默认标签页ID并更新
        if hasattr(self.paste_window, 'current_tab_id') and self.paste_window.current_tab_id is not None:
            self.paste_window.update_tab_records(self.paste_window.current_tab_id, records)
        else:
            # 存储记录供后续使用
            self.paste_window.current_records[0] = records
            # 如果列表控件已创建，更新它
            list_widget = self.paste_window.get_list_widget_for_tab(0)
            if list_widget:
                self.paste_window.update_tab_records(0, records)

    def on_tabs_loaded(self, tabs: List[Dict]):
        """标签页加载完成，加载所有标签页的记录"""
        self.paste_window.update_tabs(tabs)
        # 加载所有标签页的记录
        if tabs:
            default_tab = next((t for t in tabs if t['is_default']), tabs[0])
            self.paste_window.current_tab_id = default_tab['id']
            # 加载所有标签页的记录，而不仅是默认页
            for tab in tabs:
                self.db_worker.add_task({'type': 'load_tab_records', 'tab_id': tab['id']})
        # 延迟恢复焦点，确保界面更新完成
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(100, self.paste_window._set_focus_to_current_tab)

    def on_tab_records_loaded(self, tab_id: int, records: List[Dict]):
        """标签页记录加载完成"""
        self.paste_window.update_tab_records(tab_id, records)

    def on_tab_created(self, tab_id: int, name: str):
        """标签页创建完成"""
        logger.info("标签页创建完成: %s (ID: %s)", name, tab_id)
        # 重新加载标签页
        self.db_worker.add_task({'type': 'load_tabs'})

    def on_tab_renamed(self, tab_id: int, new_name: str):
        """标签页重命名完成"""
        logger.info("标签页重命名完成: ID %s -> %s", tab_id, new_name)
        # 重新加载标签页
        self.db_worker.add_task({'type': 'load_tabs'})

    def on_tab_deleted(self, tab_id: int):
        """标签页删除完成"""
        logger.info("标签页删除完成: ID %s", tab_id)
        # 重新加载标签页
        self.db_worker.add_task({'type': 'load_tabs'})

    def on_record_pinned(self, record_id: int, tab_id: int):
        """记录固定完成"""
        logger.info("记录 %s 已固定到标签页 %s", record_id, tab_id)
        # 刷新目标标签页的记录
        self.db_worker.add_task({'type': 'load_tab_records', 'tab_id': tab_id})
        # 刷新默认页，以更新固定记录的颜色（变蓝）
        default_tab = next((t for t in self.paste_window.tabs if t['is_default']), None)
        if default_tab:
            self.db_worker.add_task({'type': 'load_tab_records', 'tab_id': default_tab['id']})

    def on_record_unpinned(self, record_id: int, tab_id: int):
        """记录取消固定完成"""
        logger.info("记录 %s 已从标签页 %s 移除", record_id, tab_id)
        # 刷新当前标签页
        self.db_worker.add_task({'type': 'load_tab_records', 'tab_id': tab_id})
        # 刷新默认页，以更新取消固定记录的颜色（恢复为黑色）
        default_tab = next((t for t in self.paste_window.tabs if t['is_default']), None)
        if default_tab:
            self.db_worker.add_task({'type': 'load_tab_records', 'tab_id': default_tab['id']})

    def on_record_deleted(self, record_id: int):
        """记录删除完成"""
        logger.info("记录 %s 已删除", record_id)
        # 刷新当前标签页
        current_tab_id = self.paste_window.current_tab_id
        if current_tab_id is not None:
            self.db_worker.add_task({'type': 'load_tab_records', 'tab_id': current_tab_id})

    def on_record_moved(self, record_id: int, from_tab_id: int, to_tab_id: int):
        """记录移动完成"""
        logger.info("记录 %s 从标签页 %s 移动到 %s", record_id, from_tab_id, to_tab_id)
        # 刷新两个标签页
        self.db_worker.add_task({'type': 'load_tab_records', 'tab_id': from_tab_id})
        self.db_worker.add_task({'type': 'load_tab_records', 'tab_id': to_tab_id})

    def on_load_tabs_requested(self):
        """请求加载标签页"""
        self.db_worker.add_task({'type': 'load_tabs'})

    def on_load_tab_records_requested(self, tab_id: int):
        """请求加载标签页记录"""
        self.db_worker.add_task({'type': 'load_tab_records', 'tab_id': tab_id})

    def on_create_tab_requested(self, name: str):
        """请求创建标签页"""
        self.db_worker.add_task({'type': 'create_tab', 'name': name})

    def on_rename_tab_requested(self, tab_id: int, new_name: str):
        """请求重命名标签页"""
        self.db_worker.add_task({'type': 'rename_tab', 'tab_id': tab_id, 'name': new_name})

    def on_delete_tab_requested(self, tab_id: int):
        """请求删除标签页"""
        self.db_worker.add_task({'type': 'delete_tab', 'tab_id': tab_id})

    def on_pin_record_requested(self, record_id: int, tab_id: int):
        """请求固定记录"""
        self.db_worker.add_task({'type': 'pin_record', 'record_id': record_id, 'tab_id': tab_id})

    def on_unpin_record_requested(self, record_id: int, tab_id: int):
        """请求取消固定记录"""
        self.db_worker.add_task({'type': 'unpin_record', 'record_id': record_id, 'tab_id': tab_id})

    def on_delete_record_requested(self, record_id: int):
        """请求删除记录（默认标签页）"""
        # 获取当前选中行，用于删除后恢复选择位置
        list_widget = self.paste_window.get_list_widget_for_tab(self.paste_window.current_tab_id)
        if list_widget:
            current_row = list_widget.currentRow()
            # 存储要删除的记录ID和当前行号
            self._pending_delete_info = {'record_id': record_id, 'row': current_row}
        else:
            self._pending_delete_info = {'record_id': record_id, 'row': 0}
        self.db_worker.add_task({'type': 'delete_record', 'record_id': record_id})

    def on_record_deleted(self, record_id: int):
        """记录删除完成，刷新UI并选中上一项"""
        # 从当前记录中移除该记录
        for tab_id, records in list(self.paste_window.current_records.items()):
            self.paste_window.current_records[tab_id] = [
                r for r in records if r['id'] != record_id
            ]
        # 刷新当前标签页显示
        if self.paste_window.current_tab_id is not None:
            current_tab_id = self.paste_window.current_tab_id
            self.paste_window.load_tab_records_requested.emit(current_tab_id)
            # 删除后选中上一项（或保持合理位置）
            if hasattr(self, '_pending_delete_info') and self._pending_delete_info['record_id'] == record_id:
                deleted_row = self._pending_delete_info['row']
                # 选中上一项，如果删除的是第一项则选中新的第一项
                select_row = max(0, deleted_row - 1)
                # 延迟执行以确保列表已刷新
                from PyQt5.QtCore import QTimer
                QTimer.singleShot(100, lambda: self._restore_selection_after_delete(current_tab_id, select_row))

    def _restore_selection_after_delete(self, tab_id: int, row: int):
        """删除记录后恢复选择位置"""
        if self.paste_window.current_tab_id != tab_id:
            return
        list_widget = self.paste_window.get_list_widget_for_tab(tab_id)
        if list_widget:
            # 确保行号在有效范围内
            count = list_widget.count()
            if count > 0:
                select_row = min(row, count - 1)
                list_widget.setCurrentRow(select_row)
                # 清除待删除信息
                if hasattr(self, '_pending_delete_info'):
                    delattr(self, '_pending_delete_info')

    def on_move_record_requested(self, record_id: int, from_tab_id: int, to_tab_id: int):
        """请求移动记录"""
        self.db_worker.add_task({'type': 'move_pinned_record', 'record_id': record_id,
                                 'from_tab_id': from_tab_id, 'to_tab_id': to_tab_id})

    def on_reorder_tabs_requested(self, tab_orders: List[Tuple[int, int]]):
        """请求重新排序标签页"""
        self.db_worker.add_task({'type': 'reorder_tabs', 'tab_orders': tab_orders})

    def on_reorder_records_requested(self, tab_id: int, record_orders: List[Tuple[int, int]]):
        """请求重新排序固定记录"""
        self.db_worker.add_task({'type': 'reorder_pinned_records', 'tab_id': tab_id, 'record_orders': record_orders})

    def on_batch_pin_requested(self, record_ids: List[int], tab_id: int):
        """请求批量固定记录"""
        logger.info("批量固定 %s 条记录到标签页 %s", len(record_ids), tab_id)
        for record_id in record_ids:
            self.db_worker.add_task({'type': 'pin_record', 'record_id': record_id, 'tab_id': tab_id})

    def on_batch_unpin_requested(self, record_ids: List[int], tab_id: int):
        """请求批量取消固定记录"""
        logger.info("批量从标签页 %s 移除 %s 条记录", tab_id, len(record_ids))
        for record_id in record_ids:
            self.db_worker.add_task({'type': 'unpin_record', 'record_id': record_id, 'tab_id': tab_id})

    def on_batch_delete_requested(self, record_ids: List[int]):
        """请求批量删除记录"""
        logger.info("批量删除 %s 条记录", len(record_ids))
        for record_id in record_ids:
            self.db_worker.add_task({'type': 'delete_record', 'record_id': record_id})

    def on_paste_requested(self, record_id: int, is_plain_text: bool):
        """处理粘贴请求"""
        # 在所有标签页中查找记录
        record = None
        for records in self.paste_window.current_records.values():
            for r in records:
                if r['id'] == record_id:
                    record = r
                    break
            if record:
                break

        if record:
            if is_plain_text:
                self.clipboard_manager.set_content(plain_text=record['plain_text'])
                logger.debug("粘贴纯文本 (ID: %s)", record_id)
            else:
                self.clipboard_manager.set_content(
                    html=record['html_content'],
                    plain_text=record['plain_text']
                )
                logger.debug("粘贴HTML格式 (ID: %s)", record_id)

            # 模拟粘贴
            QTimer.singleShot(100, KeyboardSimulator.simulate_paste)
            self.paste_window.hide()
            self.paste_window_visible = False

    def on_paste_window_closed(self):
        """粘贴窗口关闭"""
        self.paste_window_visible = False

    def on_record_saved(self, record_id: int):
        """记录保存成功"""
        logger.debug("剪贴板内容已保存 (ID: %s)", record_id)
        # 刷新当前标签页的记录
        if hasattr(self.paste_window, 'current_tab_id') and self.paste_window.current_tab_id is not None:
            self.db_worker.add_task({'type': 'load_tab_records', 'tab_id': self.paste_window.current_tab_id})
        else:
            # 如果没有当前标签页，刷新默认标签页
            self.db_worker.add_task({'type': 'load_tabs'})

    def on_clear_all_requested(self):
        """处理清理所有默认页记录请求（带二次确认）"""
        logger.info("开始显示清理确认对话框")
        try:
            # 创建确认对话框
            self._cleanup_msg_box = QMessageBox(self)
            self._cleanup_msg_box.setWindowTitle('确认清理')
            self._cleanup_msg_box.setText('确定要清理所有默认页记录吗？\n\n注意：被固定到自定义标签页的记录将保留。')
            self._cleanup_msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            self._cleanup_msg_box.setDefaultButton(QMessageBox.No)
            self._cleanup_msg_box.setWindowFlag(Qt.WindowStaysOnTopHint, True)

            # 对话框居中于主窗口
            if self.isVisible():
                geo = self._cleanup_msg_box.geometry()
                geo.moveCenter(self.geometry().center())
                self._cleanup_msg_box.setGeometry(geo)

            # 使用 finished 信号异步处理结果，避免 exec_() 的嵌套事件循环问题
            self._cleanup_msg_box.finished.connect(self._on_cleanup_dialog_finished)
            self._cleanup_msg_box.show()
            logger.info("对话框已显示（异步模式）")
        except Exception as e:
            logger.exception("清理确认对话框异常: %s", e)
            self._cleanup_msg_box = None

    def _on_cleanup_dialog_finished(self, result: int):
        """清理对话框完成后的回调"""
        logger.info("对话框 finished 信号: %s (Yes=%s, No=%s)", result, QMessageBox.Yes, QMessageBox.No)

        # 清理引用，避免内存泄漏
        self._cleanup_msg_box = None

        # 只处理 Yes 按钮点击，其他情况（No、关闭、Esc等）都视为取消
        if result == QMessageBox.Yes:
            logger.info("用户确认清理所有默认页记录")
            # 延迟执行，确保对话框完全关闭，避免竞态条件
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(50, self._do_clear_all_records)
        else:
            logger.info("用户取消清理操作 (result=%s)", result)

    def _do_clear_all_records(self):
        """执行清理记录的的实际操作"""
        try:
            logger.info("开始执行清理记录任务")
            self.db_worker.add_task({'type': 'clear_all'})
            logger.info("已发送 clear_all 任务到数据库工作线程")
        except Exception as e:
            logger.exception("发送 clear_all 任务异常: %s", e)

    def trigger_cleanup(self):
        """触发清理任务"""
        logger.info("触发过期记录清理")
        self.db_worker.add_task({'type': 'cleanup'})

    def on_cleanup_done(self, count: int):
        """清理完成，刷新UI"""
        logger.info("on_cleanup_done 被调用, count=%s", count)
        try:
            if count > 0:
                logger.info("已清理 %s 条记录", count)
                # 刷新当前标签页显示
                current_tab_id = self.paste_window.current_tab_id
                logger.info("当前标签页ID: %s", current_tab_id)
                if current_tab_id is not None:
                    self.db_worker.add_task({'type': 'load_tab_records', 'tab_id': current_tab_id})
                # 同时刷新默认页
                default_tab = next((t for t in self.paste_window.tabs if t['is_default']), None)
                logger.info("默认标签页: %s", default_tab)
                if default_tab and default_tab['id'] != current_tab_id:
                    self.db_worker.add_task({'type': 'load_tab_records', 'tab_id': default_tab['id']})
            logger.info("on_cleanup_done 处理完成")
        except Exception as e:
            logger.exception("on_cleanup_done 异常: %s", e)

    def on_db_error(self, error_msg: str):
        """数据库错误"""
        logger.error("数据库错误: %s", error_msg)

    def _is_first_instance(self) -> bool:
        """检查是否是第一个实例 - 使用 QLocalSocket/Server 实现单实例"""
        from PyQt5.QtNetwork import QLocalSocket, QLocalServer

        socket = QLocalSocket()
        socket.connectToServer("copyU_single_instance")
        if socket.waitForConnected(500):
            # 连接成功，说明已有实例在运行
            socket.close()
            return False
        socket.close()

        # 无法连接，创建服务器
        self._local_server = QLocalServer()
        self._local_server.listen("copyU_single_instance")
        return True

    def exec_(self):
        """运行应用程序"""
        try:
            result = super().exec_()
            logger.info("exec_ 返回: %s", result)
            return result
        except Exception as e:
            logger.exception("exec_ 异常: %s", e)
            raise
        finally:
            self.cleanup()

    def cleanup(self):
        """清理资源"""
        logger.info("正在关闭 copyU...")

        # 保存窗口大小
        try:
            size = self.paste_window.size()
            self.config.setint('UI', 'window_width', size.width())
            self.config.setint('UI', 'window_height', size.height())
            self.config.save_config()
            logger.info("窗口大小已保存: %sx%s", size.width(), size.height())
        except Exception as e:
            logger.exception("保存窗口大小失败: %s", e)

        self.hotkey_manager.stop()
        self.db_worker.stop()
        self.tray_icon.hide()


def main():
    """主函数"""
    # 设置高DPI支持
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = CopyUApp(sys.argv)
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()

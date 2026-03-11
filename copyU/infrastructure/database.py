# -*- coding: utf-8 -*-
"""
数据库模块

提供数据库连接管理和后台数据库操作功能。
"""

import sqlite3
import time
import threading
import queue
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, Dict, List

from PyQt5.QtCore import QThread, pyqtSignal

from copyU.core.metrics import metrics
from copyU.infrastructure.logging_config import logger


# 常量定义
DB_TIMEOUT_SECONDS = 5  # 数据库连接超时时间
DB_BUSY_TIMEOUT_MS = 3000  # 数据库忙等待超时（毫秒）
MAX_TASK_QUEUE_SIZE = 1000  # 任务队列最大长度


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

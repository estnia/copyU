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
import configparser
import logging
import shutil
from collections import deque
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QSystemTrayIcon, QMenu, QAction, QLabel, QAbstractItemView, QTabWidget,
    QTabBar, QPushButton, QInputDialog, QMessageBox
)
from PyQt5.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QMimeData, QPoint, QSize, QObject, QRect, QProcess
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


# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('copyU')

# 常量定义
MAX_CLIPBOARD_SIZE = 2_000_000  # 2MB 剪贴板内容大小限制


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

    def __init__(self, db_path: str, max_age_days: int, max_size_mb: int):
        super().__init__()
        self.db_path = db_path
        self.max_age_days = max_age_days
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self.running = True

        # 任务队列 - 使用 deque 限制长度防止内存无限增长
        self.task_queue = deque(maxlen=1000)
        self.queue_lock = threading.Lock()
        self.queue_event = threading.Event()

        self.init_database()

    def init_database(self):
        """初始化数据库 - 使用 context manager 确保连接正确关闭"""
        with sqlite3.connect(self.db_path, timeout=5) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
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
        """主循环，处理数据库任务"""
        while self.running:
            self.queue_event.wait(timeout=60)  # 等待任务或超时
            self.queue_event.clear()

            if not self.running:
                break

            # 处理任务队列
            with self.queue_lock:
                tasks = list(self.task_queue)
                self.task_queue.clear()

            for task in tasks:
                try:
                    self.execute_task(task)
                except (sqlite3.Error, OSError, ValueError) as e:
                    self.error_occurred.emit(str(e))

            # 定期清理
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
        with sqlite3.connect(self.db_path, timeout=5) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
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
        """从数据库加载记录 - 使用 context manager"""
        limit = task.get('limit', 50)
        search_text = task.get('search', '')

        with sqlite3.connect(self.db_path, timeout=5) as conn:
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

        with sqlite3.connect(self.db_path, timeout=5) as conn:
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

    def _load_tabs(self, task: Dict):
        """加载所有标签页 - 使用 context manager"""
        with sqlite3.connect(self.db_path, timeout=5) as conn:
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

        with sqlite3.connect(self.db_path, timeout=5) as conn:
            cursor = conn.cursor()

            # 检查是否是默认页
            cursor.execute('SELECT is_default FROM tabs WHERE id = ?', (tab_id,))
            result = cursor.fetchone()
            is_default = result and result[0] == 1

            if is_default:
                # 默认页显示所有记录（按时间倒序）
                cursor.execute('''
                    SELECT id, html_content, plain_text, timestamp, app_name
                    FROM clipboard_records
                    ORDER BY timestamp DESC
                    LIMIT ?
                ''', (limit,))
            else:
                # 自定义标签页显示固定的记录（按固定排序）
                cursor.execute('''
                    SELECT r.id, r.html_content, r.plain_text, r.timestamp, r.app_name
                    FROM clipboard_records r
                    JOIN pinned_records p ON r.id = p.record_id
                    WHERE p.tab_id = ?
                    ORDER BY p.sort_order ASC, p.pinned_at DESC
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
                'display_time': datetime.fromtimestamp(row[3]).strftime('%m-%d %H:%M')
            })

        self.tab_records_loaded.emit(tab_id, result)

    def _create_tab(self, task: Dict):
        """创建新标签页 - 使用 context manager"""
        name = task.get('name', '新标签页')

        with sqlite3.connect(self.db_path, timeout=5) as conn:
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

        with sqlite3.connect(self.db_path, timeout=5) as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE tabs SET name = ? WHERE id = ? AND is_default = 0', (name, tab_id))

        self.tab_renamed.emit(tab_id, name)

    def _delete_tab(self, task: Dict):
        """删除标签页（同时删除关联的固定记录）- 使用 context manager"""
        tab_id = task.get('tab_id')

        with sqlite3.connect(self.db_path, timeout=5) as conn:
            cursor = conn.cursor()

            # 先删除关联的固定记录
            cursor.execute('DELETE FROM pinned_records WHERE tab_id = ?', (tab_id,))

            # 再删除标签页
            cursor.execute('DELETE FROM tabs WHERE id = ? AND is_default = 0', (tab_id,))

        self.tab_deleted.emit(tab_id)

    def _reorder_tabs(self, task: Dict):
        """重新排序标签页 - 使用 context manager"""
        tab_orders = task.get('tab_orders', [])  # [(tab_id, new_order), ...]

        with sqlite3.connect(self.db_path, timeout=5) as conn:
            cursor = conn.cursor()

            for tab_id, new_order in tab_orders:
                cursor.execute('UPDATE tabs SET sort_order = ? WHERE id = ? AND is_default = 0', (new_order, tab_id))

        self.tabs_reordered.emit()

    def _pin_record(self, task: Dict):
        """固定记录到标签页 - 使用 context manager"""
        record_id = task.get('record_id')
        tab_id = task.get('tab_id')

        with sqlite3.connect(self.db_path, timeout=5) as conn:
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

        with sqlite3.connect(self.db_path, timeout=5) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM pinned_records WHERE record_id = ? AND tab_id = ?', (record_id, tab_id))

        self.record_unpinned.emit(record_id, tab_id)

    def _move_pinned_record(self, task: Dict):
        """移动固定记录到其他标签页 - 使用 context manager"""
        record_id = task.get('record_id')
        from_tab_id = task.get('from_tab_id')
        to_tab_id = task.get('to_tab_id')

        with sqlite3.connect(self.db_path, timeout=5) as conn:
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

        with sqlite3.connect(self.db_path, timeout=5) as conn:
            cursor = conn.cursor()

            for record_id, new_order in record_orders:
                cursor.execute('UPDATE pinned_records SET sort_order = ? WHERE record_id = ? AND tab_id = ?',
                             (new_order, record_id, tab_id))

        self.pinned_records_reordered.emit(tab_id)

    def cleanup_old_records(self):
        """公开方法：触发清理"""
        self._cleanup_records()

    def add_task(self, task: Dict):
        """添加任务到队列"""
        with self.queue_lock:
            self.task_queue.append(task)
        self.queue_event.set()

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
    move_record_requested = pyqtSignal(int, int, int)  # 请求移动记录 (record_id, from_tab_id, to_tab_id)
    reorder_records_requested = pyqtSignal(int, list)  # 请求重新排序固定记录 (tab_id, [(record_id, order), ...])

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

        # 菜单互斥锁
        self._tab_menu_open = False

        # 移除角落的添加按钮，改用右键菜单
        self.add_tab_button = None

        layout.addWidget(self.tab_widget)

        # 设置标签页样式
        self.tab_widget.setStyleSheet(f'''
            QTabWidget::pane {{
                border: none;
                background: transparent;
            }}
            QTabBar::tab {{
                background: rgba(220, 220, 220, 180);
                border-radius: 4px 4px 0 0;
                padding: 6px 12px;
                margin-right: 2px;
                font-size: {font_size}px;
                color: #333;
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

        # 快捷键提示标签
        self.title_label = QLabel('双击/Enter粘贴 | Shift+Enter纯文本 | ↑↓选择 | 右键管理 | Esc关闭')
        self.title_label.setAlignment(Qt.AlignCenter | Qt.AlignVCenter)
        self.title_label.setFont(QFont('Noto Sans CJK SC', font_size - 2))
        bottom_layout.addWidget(self.title_label)

        layout.addWidget(bottom_bar)

        # 设置透明度
        opacity = self.config.getfloat('UI', 'window_opacity', 0.95)
        self.setWindowOpacity(opacity)

    def setup_style(self):
        """设置UOS深度风格样式"""
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
        list_widget.setSelectionMode(QAbstractItemView.SingleSelection)
        list_widget.setContextMenuPolicy(Qt.CustomContextMenu)

        # 设置行高以适应字号（字号+12像素作为最小高度）
        item_height = max(28, font_size + 14)
        list_widget.setStyleSheet(f'''
            QListWidget::item {{
                min-height: {item_height}px;
                padding: 2px;
            }}
        ''')

        # 连接信号
        list_widget.itemDoubleClicked.connect(lambda item, tid=tab_id: self.on_item_double_clicked(item, tid))
        list_widget.customContextMenuRequested.connect(lambda pos, tid=tab_id: self.on_list_context_menu(pos, tid))

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
        """列表右键菜单"""
        list_widget = self.get_list_widget_for_tab(tab_id)
        if not list_widget:
            return

        item = list_widget.itemAt(position)
        if not item:
            return

        record_id = item.data(Qt.UserRole)

        menu = QMenu(self)
        font_size = self.config.getint('UI', 'font_size', 12)
        menu.setFont(QFont('Noto Sans CJK SC', font_size))

        # 粘贴选项
        paste_action = QAction('粘贴原格式', self)
        paste_action.triggered.connect(lambda: self.paste_requested.emit(record_id, False))
        menu.addAction(paste_action)

        paste_text_action = QAction('粘贴纯文本', self)
        paste_text_action.triggered.connect(lambda: self.paste_requested.emit(record_id, True))
        menu.addAction(paste_text_action)

        menu.addSeparator()

        # 获取当前标签页信息
        current_tab = next((t for t in self.tabs if t['id'] == tab_id), None)

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
            unpin_action.triggered.connect(lambda: self.unpin_record_requested.emit(record_id, tab_id))
            menu.addAction(unpin_action)

        menu.exec_(list_widget.mapToGlobal(position))

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

            # 确认删除 - 设置置顶标志
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle('确认删除')
            msg_box.setText(f'确定要删除标签页 "{tab_name}" 吗？\n其中的记录将回到默认页。')
            msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            msg_box.setWindowFlags(msg_box.windowFlags() | Qt.WindowStaysOnTopHint)
            reply = msg_box.exec_()

            if reply == QMessageBox.Yes:
                self.delete_tab_requested.emit(tab_id)
        except (AttributeError, TypeError) as e:
            print(f"标签页关闭请求错误: {e}")

    def on_tab_changed(self, index: int):
        """标签页切换 - 保留选择光标位置"""
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
        except (AttributeError, TypeError):
            pass

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

    def on_delete_tab_clicked(self, tab_id: int):
        """删除标签页"""
        # 确认删除
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle('确认删除')
        msg_box.setText('确定要删除该标签页吗？其中的记录将回到默认页。')
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint | Qt.Tool)
        reply = msg_box.exec_()

        if reply == QMessageBox.Yes:
            self.delete_tab_requested.emit(tab_id)

    def on_add_tab_clicked(self):
        """添加新标签页"""
        # 检查自定义标签页数量上限
        custom_tabs = [t for t in self.tabs if not t['is_default']]
        if len(custom_tabs) >= 3:
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle('达到上限')
            msg_box.setText('最多只能创建3个自定义标签页。')
            msg_box.setStandardButtons(QMessageBox.Ok)
            msg_box.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint | Qt.Tool)
            msg_box.exec_()
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

        for record in records:
            text = record['plain_text'].replace('\n', ' ').strip()
            if len(text) > 50:
                text = text[:50] + '...'

            item = QListWidgetItem()
            item.setData(Qt.UserRole, record['id'])
            item.setToolTip(record['plain_text'][:200])

            # 使用富文本 - 时间字号不变，记录文字增大1号
            time_str = record['display_time']
            html_text = f'<span style="font-size:{time_font_size}px; color:#888888;">[{time_str}]</span> <span style="font-size:{text_font_size}px;">{text}</span>'

            label = QLabel(html_text)
            label.setWordWrap(False)
            label.setStyleSheet("background: transparent; padding: 4px;")
            label.setAttribute(Qt.WA_TransparentForMouseEvents)

            list_widget.addItem(item)
            list_widget.setItemWidget(item, label)

        # 默认选中第一项
        if list_widget.count() > 0:
            list_widget.setCurrentRow(0)

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
                record_id = current_item.data(Qt.UserRole)
                is_plain = (modifiers == Qt.ShiftModifier)
                self.paste_requested.emit(record_id, is_plain)

        elif key == Qt.Key_1 and modifiers == Qt.ControlModifier:
            # Ctrl+1: 粘贴纯文本
            if current_item:
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
        """窗口隐藏事件"""
        # 关闭标签页右键菜单
        if self._tab_menu and self._tab_menu.isVisible():
            self._tab_menu.close()
            self._tab_menu = None
        self.closed.emit()
        super().hideEvent(event)

    def eventFilter(self, obj, event):
        """事件过滤器：检测窗口失焦和点击菜单外部"""
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
    """键盘模拟器，用于发送粘贴命令 - 使用异步 QProcess 避免阻塞主线程"""

    _instance = None
    _xdotool_process = None
    _timer = None
    _finished_callback = None

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
            # 确保进程不在运行中
            if self._xdotool_process.state() != QProcess.NotRunning:
                self._xdotool_process.kill()
                self._xdotool_process.waitForFinished(100)

            self._finished_callback = callback
            self._xdotool_process.setArguments(args)

            # 设置超时定时器
            self._timer = QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.timeout.connect(lambda: self._on_timeout(args))
            self._timer.start(timeout_ms)

            self._xdotool_process.start()

        except (OSError, IOError, RuntimeError) as e:
            self._logger.warning("QProcess 执行失败: %s", e)
            self._finished_callback = None
            callback(False)

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

    @staticmethod
    def simulate_paste():
        """模拟 Ctrl+V 粘贴 - 非阻塞异步执行"""
        import time
        import shutil
        import logging

        logger = logging.getLogger('copyU.keyboard')

        # 先等待一小段时间确保用户已释放按键
        time.sleep(0.05)

        # 优先使用 xdotool（Linux X11 环境）
        if shutil.which("xdotool"):
            simulator = KeyboardSimulator.get_instance()

            # 使用 QEventLoop 实现同步等待但不阻塞主事件循环
            from PyQt5.QtCore import QEventLoop

            loop = QEventLoop()
            keyup_success = [False]

            def on_keyup_done(success):
                keyup_success[0] = success
                loop.quit()

            # 异步执行 keyup，通过事件循环等待
            simulator.run_xdotool_async(["keyup", "ctrl", "alt", "shift", "meta"], on_keyup_done, 300)
            QTimer.singleShot(350, loop.quit)  # 保险超时
            loop.exec_()

            if keyup_success[0]:
                time.sleep(0.02)

                loop = QEventLoop()
                key_success = [False]

                def on_key_done(success):
                    key_success[0] = success
                    loop.quit()

                simulator.run_xdotool_async(["key", "ctrl+v"], on_key_done, 300)
                QTimer.singleShot(350, loop.quit)
                loop.exec_()

                if key_success[0]:
                    logger.debug("使用 xdotool 模拟粘贴成功")
                    return
                else:
                    logger.warning("xdotool key 命令失败，尝试 pyautogui")
            else:
                logger.warning("xdotool keyup 命令失败，尝试 pyautogui")
        else:
            logger.info("xdotool 未找到，尝试 pyautogui")

        # 回退到 pyautogui
        try:
            import pyautogui
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
        except ImportError:
            logger.error("无法模拟键盘，请安装 xdotool 或 pyautogui")


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
        self.cleanup_action = QAction('清理过期记录', self)
        self.menu.addAction(self.cleanup_action)

        self.menu.addSeparator()

        # 退出
        self.quit_action = QAction('退出', self)
        self.quit_action.triggered.connect(QApplication.instance().quit)
        self.menu.addAction(self.quit_action)

        self.setContextMenu(self.menu)

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

        # 初始化配置
        self.config = ConfigManager()

        # 初始化数据库工作线程
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
        self.paste_window.move_record_requested.connect(self.on_move_record_requested)
        self.paste_window.reorder_tabs_requested.connect(self.on_reorder_tabs_requested)
        self.paste_window.reorder_records_requested.connect(self.on_reorder_records_requested)

        # 初始化全局热键
        self.hotkey_manager = GlobalHotkeyManager(self.config)
        self.hotkey_manager.show_triggered.connect(self.toggle_paste_window)
        self.hotkey_manager.start()

        # 连接系统剪贴板变化信号（复用系统 Ctrl+C/V）
        self.clipboard().dataChanged.connect(self.on_clipboard_changed)

        # 初始化系统托盘
        self.tray_icon = TrayIcon(self)
        self.tray_icon.show_action.triggered.connect(self.show_paste_window)
        self.tray_icon.cleanup_action.triggered.connect(self.trigger_cleanup)
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
        logger.info("  Ctrl+~     - 显示/隐藏剪贴板窗口")
        logger.info("  Ctrl+C     - 复制（系统自动保存到默认页）")
        logger.info("  ↑/↓        - 选择历史记录")
        logger.info("  Enter      - 粘贴原格式")
        logger.info("  Shift+Enter- 粘贴纯文本")
        logger.info("  双击       - 直接粘贴原格式")
        logger.info("  Esc        - 关闭窗口")
        logger.info("")
        logger.info("标签页功能:")
        logger.info("  - 默认页：所有新记录自动保存至此")
        logger.info("  - 自定义标签页：可创建最多3个，可拖放排序")
        logger.info("  - 右键记录：固定到/移动到其他标签页")
        logger.info("  - 自定义标签页内记录可拖动排序")
        logger.info("=" * 40)

    def on_clipboard_changed(self):
        """系统剪贴板变化时自动保存（复用系统 Ctrl+C/V）"""
        # 获取剪贴板内容
        html_content, plain_text = self.clipboard_manager.get_content()

        if not plain_text and not html_content:
            return

        # 大小限制检查 - 防止极大文本导致卡顿
        content_size = len(plain_text) if plain_text else 0
        if content_size > MAX_CLIPBOARD_SIZE:
            logger.warning("剪贴板内容过大 (%s 字符)，已跳过保存", content_size)
            return

        # 检查重复
        if self.clipboard_manager.is_duplicate(plain_text):
            return

        # 提交保存任务
        self.db_worker.add_task({
            'type': 'save',
            'html_content': html_content,
            'plain_text': plain_text,
            'app_name': ''
        })

    def toggle_paste_window(self):
        """Ctrl+~ 热键处理: 切换显示/隐藏剪贴板窗口"""
        # 高频调用节流 - 防止 50ms 内重复执行
        now = time.monotonic()
        if hasattr(self, '_last_toggle_time') and now - self._last_toggle_time < 0.05:
            return
        self._last_toggle_time = now

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
        """标签页加载完成"""
        self.paste_window.update_tabs(tabs)
        # 加载第一个标签页的记录并设置当前标签页
        if tabs:
            default_tab = next((t for t in tabs if t['is_default']), tabs[0])
            self.paste_window.current_tab_id = default_tab['id']
            self.db_worker.add_task({'type': 'load_tab_records', 'tab_id': default_tab['id']})

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

    def on_record_unpinned(self, record_id: int, tab_id: int):
        """记录取消固定完成"""
        logger.info("记录 %s 已从标签页 %s 移除", record_id, tab_id)
        # 刷新当前标签页
        self.db_worker.add_task({'type': 'load_tab_records', 'tab_id': tab_id})

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

    def trigger_cleanup(self):
        """触发清理任务"""
        logger.info("触发过期记录清理")
        self.db_worker.add_task({'type': 'cleanup'})

    def on_cleanup_done(self, count: int):
        """清理完成"""
        if count > 0:
            logger.info("已清理 %s 条过期记录", count)

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
        import traceback
        try:
            result = super().exec_()
            logger.debug("exec_ 返回: %s", result)
            return result
        except (OSError, RuntimeError, AttributeError) as e:
            logger.exception("exec_ 异常: %s", e)
            raise
        finally:
            self.cleanup()

    def cleanup(self):
        """清理资源"""
        logger.info("正在关闭 copyU...")
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

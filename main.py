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
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QSystemTrayIcon, QMenu, QAction, QLabel, QAbstractItemView
)
from PyQt5.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QMimeData, QPoint, QSize, QObject
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
    print("警告: pynput 未安装，尝试使用 system_hotkey")

try:
    from system_hotkey import SystemHotkey
    SYSTEM_HOTKEY_AVAILABLE = True
except ImportError:
    SYSTEM_HOTKEY_AVAILABLE = False


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

    def __init__(self, db_path: str, max_age_days: int, max_size_mb: int):
        super().__init__()
        self.db_path = db_path
        self.max_age_days = max_age_days
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self.running = True

        # 任务队列
        self.task_queue = []
        self.queue_lock = threading.Lock()
        self.queue_event = threading.Event()

        self.init_database()

    def init_database(self):
        """初始化数据库"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
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
        conn.commit()
        conn.close()

    def run(self):
        """主循环，处理数据库任务"""
        while self.running:
            self.queue_event.wait(timeout=60)  # 等待任务或超时
            self.queue_event.clear()

            if not self.running:
                break

            # 处理任务队列
            with self.queue_lock:
                tasks = self.task_queue.copy()
                self.task_queue.clear()

            for task in tasks:
                try:
                    self.execute_task(task)
                except Exception as e:
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

    def _save_record(self, task: Dict):
        """保存记录到数据库"""
        html_content = task.get('html_content', '')
        plain_text = task.get('plain_text', '')
        app_name = task.get('app_name', '')

        # 检查内容大小
        content_size = len(html_content.encode('utf-8')) + len(plain_text.encode('utf-8'))
        if content_size > self.max_size_bytes:
            self.error_occurred.emit(f"内容超过 {self.max_size_bytes // 1024 // 1024}MB 限制，跳过保存")
            return

        # 检查是否重复（最近1分钟内相同内容）
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        one_minute_ago = time.time() - 60
        cursor.execute('''
            SELECT id FROM clipboard_records
            WHERE plain_text = ? AND timestamp > ?
        ''', (plain_text, one_minute_ago))

        if cursor.fetchone():
            conn.close()
            return  # 重复内容，跳过

        # 插入新记录
        cursor.execute('''
            INSERT INTO clipboard_records (html_content, plain_text, timestamp, app_name, content_size)
            VALUES (?, ?, ?, ?, ?)
        ''', (html_content, plain_text, time.time(), app_name, content_size))

        record_id = cursor.lastrowid
        conn.commit()
        conn.close()

        self.record_saved.emit(record_id)

    def _load_records(self, task: Dict):
        """从数据库加载记录"""
        limit = task.get('limit', 50)
        search_text = task.get('search', '')

        conn = sqlite3.connect(self.db_path)
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
        conn.close()

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
        """清理过期记录"""
        cutoff_time = time.time() - (self.max_age_days * 24 * 3600)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            DELETE FROM clipboard_records WHERE timestamp < ?
        ''', (cutoff_time,))

        deleted_count = cursor.rowcount
        conn.commit()
        conn.close()

        if deleted_count > 0:
            self.cleanup_done.emit(deleted_count)

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
        """设置剪贴板内容"""
        mime_data = QMimeData()

        if html:
            mime_data.setHtml(html)
        if plain_text:
            mime_data.setText(plain_text)

        self.clipboard.setMimeData(mime_data)

    def is_duplicate(self, content: str) -> bool:
        """检查内容是否与上次相同"""
        if content == self.last_content:
            return True
        self.last_content = content
        return False


# ==================== 粘贴窗口 ====================
class PasteWindow(QWidget):
    """选择性粘贴窗口"""

    paste_requested = pyqtSignal(int, bool)  # (record_id, is_plain_text)
    closed = pyqtSignal()

    def __init__(self, config: ConfigManager, parent=None):
        super().__init__(parent)
        self.config = config
        self.records = []
        self.selected_index = -1

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

        # 窗口大小和位置
        width = self.config.getint('UI', 'window_width', 400)
        height = self.config.getint('UI', 'window_height', 300)
        self.setFixedSize(width, height)

        # 主布局
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 0)  # 底部边距设为0，与底部栏融合
        layout.setSpacing(5)

        font_size = self.config.getint('UI', 'font_size', 12)

        # 列表容器（用于浮动水印定位）
        self.list_container = QWidget()
        self.list_container.setObjectName('listContainer')
        list_container_layout = QVBoxLayout(self.list_container)
        list_container_layout.setContentsMargins(0, 0, 0, 0)
        list_container_layout.setSpacing(0)

        # 列表控件
        self.list_widget = QListWidget()
        self.list_widget.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_widget.setFont(QFont('Noto Sans CJK SC', font_size))
        self.list_widget.itemClicked.connect(self.on_item_clicked)
        self.list_widget.itemDoubleClicked.connect(self.on_item_double_clicked)
        # 确保列表可以获得焦点
        self.list_widget.setFocusPolicy(Qt.StrongFocus)
        list_container_layout.addWidget(self.list_widget)

        # 统计水印（浮动在列表右下角，绝对定位）
        self.hint_label = QLabel('共 0 条', self.list_container)
        self.hint_label.setObjectName('statsWatermark')
        self.hint_label.setAlignment(Qt.AlignCenter)
        self.hint_label.setFont(QFont('Noto Sans CJK SC', font_size - 2))
        self.hint_label.setFixedSize(70, 24)
        # 鼠标穿透，不拦截点击事件
        self.hint_label.setAttribute(Qt.WA_TransparentForMouseEvents)

        layout.addWidget(self.list_container, 1)  # 占据剩余空间

        # 底部融合栏（仅快捷键提示）
        bottom_bar = QWidget()
        bottom_bar.setObjectName('bottomBar')
        bottom_layout = QHBoxLayout(bottom_bar)
        bottom_layout.setContentsMargins(10, 6, 10, 6)
        bottom_layout.setSpacing(5)

        # 快捷键提示标签
        self.title_label = QLabel('单击/↑↓选择 | Ctrl+1纯文本 | Ctrl+Enter/双击原格式 | Esc关闭')
        self.title_label.setAlignment(Qt.AlignCenter | Qt.AlignVCenter)
        self.title_label.setFont(QFont('Noto Sans CJK SC', font_size - 2))
        bottom_layout.addWidget(self.title_label)

        layout.addWidget(bottom_bar)

        # 设置透明度
        opacity = self.config.getfloat('UI', 'window_opacity', 0.95)
        self.setWindowOpacity(opacity)

    def setup_style(self):
        """设置UOS深度风格样式"""
        self.setStyleSheet("""
            QWidget {
                background-color: rgba(245, 245, 245, 240);
                border-radius: 8px;
            }
            QListWidget {
                background-color: rgba(255, 255, 255, 200);
                border: 1px solid rgba(200, 200, 200, 150);
                border-radius: 6px;
                outline: none;
                padding: 5px;
            }
            QListWidget::item {
                background-color: transparent;
                border-radius: 4px;
                padding: 8px;
                margin: 2px;
                color: #333333;
            }
            QListWidget::item:hover {
                background-color: rgba(0, 120, 215, 30);
            }
            QListWidget::item:selected {
                background-color: rgba(0, 120, 215, 180);
                color: white;
            }
            QLabel {
                color: #666666;
                background-color: transparent;
            }
            /* 底部融合栏样式 - 与窗口融为一体 */
            QWidget#bottomBar {
                background-color: rgba(235, 235, 235, 200);
                border-bottom-left-radius: 8px;
                border-bottom-right-radius: 8px;
            }
            /* 列表容器（用于水印定位） */
            QWidget#listContainer {
                background-color: transparent;
                position: relative;
            }
            /* 统计水印样式 - 浮动在右下角 */
            QLabel#statsWatermark {
                color: rgba(100, 100, 100, 200);
                background-color: rgba(220, 220, 220, 180);
                border-radius: 12px;
                padding: 2px 8px;
            }
        """)

    def update_records(self, records: List[Dict]):
        """更新记录列表 - 统计作为最后一行内嵌显示"""
        self.records = records
        self.list_widget.clear()

        font_size = self.config.getint('UI', 'font_size', 12)
        time_font_size = max(8, font_size - 2)  # 时间字号小2号，最小为8

        for record in records:
            # 截取显示文本（前50个字符）
            text = record['plain_text'].replace('\n', ' ').strip()
            if len(text) > 50:
                text = text[:50] + '...'

            # 使用富文本格式显示，时间字号小2号
            display_text = f"[{record['display_time']}] {text}"
            item = QListWidgetItem(display_text)
            item.setData(Qt.UserRole, record['id'])
            item.setToolTip(record['plain_text'][:200])  # 悬停提示

            # 设置富文本格式，时间部分使用更小的字号
            time_str = record['display_time']
            html_text = f'<span style="font-size:{time_font_size}px; color:#888888;">[{time_str}]</span> <span style="font-size:{font_size}px;">{text}</span>'
            item.setText("")  # 清空默认文本，使用富文本
            item.setData(Qt.UserRole + 1, html_text)  # 存储富文本

            self.list_widget.addItem(item)

        # 设置项目的富文本显示
        self._update_item_display()

    def _update_item_display(self):
        """更新列表项的显示（使用富文本）"""
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            html_text = item.data(Qt.UserRole + 1)
            if html_text:
                # 创建一个 QLabel 来显示富文本
                label = QLabel(html_text)
                label.setWordWrap(False)
                label.setStyleSheet("background: transparent; padding: 4px;")
                # 设置鼠标透明，让点击事件传递到列表项
                label.setAttribute(Qt.WA_TransparentForMouseEvents)
                self.list_widget.setItemWidget(item, label)

        # 更新统计水印
        count = len(self.records)
        self.hint_label.setText(f'共 {count} 条')

        # 默认选中第一项
        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)

    def on_item_clicked(self, item: QListWidgetItem):
        """项目单击事件 - 仅选中，不粘贴"""
        self.list_widget.setCurrentItem(item)
        # 单击不自动粘贴，等待用户按键

    def on_item_double_clicked(self, item: QListWidgetItem):
        """项目双击事件 - 粘贴原格式（HTML）"""
        record_id = item.data(Qt.UserRole)
        self.paste_requested.emit(record_id, False)  # False = 原格式
        self.hide()

    def keyPressEvent(self, event: QKeyEvent):
        """键盘事件处理"""
        key = event.key()
        modifiers = event.modifiers()

        if key == Qt.Key_Escape:
            self.hide()
            self.closed.emit()

        elif key == Qt.Key_1 and modifiers == Qt.ControlModifier:
            # Ctrl+1: 粘贴纯文本
            current_item = self.list_widget.currentItem()
            if current_item:
                record_id = current_item.data(Qt.UserRole)
                self.paste_requested.emit(record_id, True)  # True = 纯文本
                self.hide()

        elif key == Qt.Key_Return or key == Qt.Key_Enter:
            if modifiers == Qt.ControlModifier:
                # Ctrl+Enter: 粘贴原格式（HTML）
                current_item = self.list_widget.currentItem()
                if current_item:
                    record_id = current_item.data(Qt.UserRole)
                    self.paste_requested.emit(record_id, False)  # False = 原格式
                    self.hide()
            # 单独的 Enter 键不做任何操作（需要配合 Ctrl）

        elif key == Qt.Key_Up:
            current_row = self.list_widget.currentRow()
            if current_row > 0:
                self.list_widget.setCurrentRow(current_row - 1)

        elif key == Qt.Key_Down:
            current_row = self.list_widget.currentRow()
            if current_row < self.list_widget.count() - 1:
                self.list_widget.setCurrentRow(current_row + 1)

        elif key == Qt.Key_Home:
            # Home 键跳到第一项
            if self.list_widget.count() > 0:
                self.list_widget.setCurrentRow(0)

        elif key == Qt.Key_End:
            # End 键跳到最后一项
            last_idx = self.list_widget.count() - 1
            if last_idx >= 0:
                self.list_widget.setCurrentRow(last_idx)

        elif key == Qt.Key_PageUp:
            # PageUp 向上翻页
            current_row = self.list_widget.currentRow()
            new_row = max(0, current_row - 5)
            self.list_widget.setCurrentRow(new_row)

        elif key == Qt.Key_PageDown:
            # PageDown 向下翻页
            current_row = self.list_widget.currentRow()
            new_row = min(self.list_widget.count() - 1, current_row + 5)
            self.list_widget.setCurrentRow(new_row)

        else:
            super().keyPressEvent(event)

    def show_at_cursor(self):
        """在鼠标位置显示窗口 - 向下偏移避免遮挡光标所在行"""
        cursor_pos = QCursor.pos()
        x = cursor_pos.x() - self.width() // 2
        y = cursor_pos.y() + 25  # 向下偏移25像素，避免遮挡光标所在行

        # 确保窗口不超出屏幕
        screen = QApplication.primaryScreen().geometry()
        x = max(0, min(x, screen.width() - self.width()))
        y = max(0, min(y, screen.height() - self.height()))

        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()

        # 确保列表获得焦点，这样上下键才能工作
        self.list_widget.setFocus()
        # 默认选中第一项
        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)

    def resizeEvent(self, event):
        """窗口大小变化时更新水印位置"""
        super().resizeEvent(event)
        # 定位水印到列表容器右下角
        if hasattr(self, 'hint_label') and hasattr(self, 'list_container'):
            container_rect = self.list_container.geometry()
            # 右下角偏移 10px
            x = container_rect.width() - self.hint_label.width() - 10
            y = container_rect.height() - self.hint_label.height() - 10
            self.hint_label.move(x, y)

    def hideEvent(self, event):
        """窗口隐藏事件"""
        self.closed.emit()
        super().hideEvent(event)

    def eventFilter(self, obj, event):
        """事件过滤器：检测窗口失焦或点击外部，自动隐藏"""
        if obj == self:
            # 窗口失焦时隐藏
            if event.type() == event.WindowDeactivate:
                if self.isVisible():
                    self.hide()
                    self.closed.emit()
                return False
        return super().eventFilter(obj, event)


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
            except Exception as e:
                print(f"pynput 启动失败: {e}")
                print("尝试使用 system_hotkey...")

        if SYSTEM_HOTKEY_AVAILABLE:
            try:
                self._start_system_hotkey()
                return
            except Exception as e:
                print(f"system_hotkey 启动失败: {e}")

        print("错误: 没有可用的全局热键库，请安装 pynput 或 system_hotkey")

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
            except Exception as e:
                # 忽略常见的热键检测噪声错误
                if "grave" not in str(e).lower():
                    print(f"热键检测错误: {e}")

        def on_release(key):
            if key == Key.ctrl_l or key == Key.ctrl_r:
                self._ctrl_pressed = False

        self.listener = Listener(on_press=on_press, on_release=on_release)
        self.listener.start()
        print("全局热键已注册: Ctrl+~ 显示/隐藏剪贴板窗口")

    def _start_system_hotkey(self):
        """使用 system_hotkey 监听热键"""
        try:
            self.hotkey_manager = SystemHotkey()
            # 尝试注册 Ctrl+~ (使用 backtick 作为备选)
            try:
                self.hotkey_manager.register(('ctrl', 'grave'), callback=self.show_triggered.emit)
                print("全局热键已注册: Ctrl+~ 显示/隐藏剪贴板窗口")
            except Exception:
                try:
                    self.hotkey_manager.register(('ctrl', '`'), callback=self.show_triggered.emit)
                    print("全局热键已注册: Ctrl+` 显示/隐藏剪贴板窗口")
                except Exception as e2:
                    print(f"system_hotkey 注册失败: {e2}")
        except Exception as e:
            print(f"system_hotkey 初始化失败: {e}")

    def stop(self):
        """停止热键监听"""
        self.running = False

        if self.listener:
            try:
                self.listener.stop()
            except Exception as e:
                print(f"停止 pynput 监听失败: {e}")

        if self.hotkey_manager:
            try:
                self.hotkey_manager.unregister(('ctrl', 'grave'))
            except Exception:
                try:
                    self.hotkey_manager.unregister(('ctrl', '`'))
                except Exception as e2:
                    print(f"停止 system_hotkey 失败: {e2}")


# ==================== 键盘模拟器 ====================
class KeyboardSimulator:
    """键盘模拟器，用于发送粘贴命令"""

    @staticmethod
    def simulate_paste():
        """模拟 Ctrl+V 粘贴 - 使用更可靠的方式避免残留按键"""
        import time

        # 先等待一小段时间确保用户已释放按键
        time.sleep(0.05)

        try:
            # 优先使用 xdotool（Linux X11 环境）
            import subprocess
            # 先释放所有可能卡住的修饰键
            subprocess.run(
                ['xdotool', 'keyup', 'ctrl', 'alt', 'shift', 'meta'],
                check=False, timeout=1
            )
            time.sleep(0.02)
            # 再发送粘贴命令
            subprocess.run(
                ['xdotool', 'key', 'ctrl+v'],
                check=False, timeout=1
            )
        except Exception:
            # 回退到 pyautogui
            try:
                import pyautogui
                # 设置短暂暂停确保按键顺序正确
                pyautogui.PAUSE = 0.01
                # 先释放所有按键
                pyautogui.keyUp('ctrl')
                pyautogui.keyUp('v')
                pyautogui.keyUp('alt')
                pyautogui.keyUp('shift')
                time.sleep(0.05)
                # 执行粘贴
                pyautogui.keyDown('ctrl')
                pyautogui.keyDown('v')
                pyautogui.keyUp('v')
                pyautogui.keyUp('ctrl')
            except ImportError:
                print("错误: 无法模拟键盘，请安装 xdotool 或 pyautogui")


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
                print(f"已启用开机启动: {desktop_file}")
            except Exception as e:
                print(f"启用开机启动失败: {e}")
                self.autostart_action.setChecked(False)
        else:
            # 删除 .desktop 文件
            try:
                if os.path.exists(desktop_file):
                    os.remove(desktop_file)
                    print("已禁用开机启动")
            except Exception as e:
                print(f"禁用开机启动失败: {e}")
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
            print("copyU 已在运行，退出新实例...")
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
        self.db_worker.start()

        # 启动时执行一次清理
        self.db_worker.add_task({'type': 'cleanup'})

        # 初始化剪贴板管理器
        self.clipboard_manager = ClipboardManager(self.clipboard())

        # 初始化粘贴窗口
        self.paste_window = PasteWindow(self.config)
        self.paste_window.paste_requested.connect(self.on_paste_requested)
        self.paste_window.closed.connect(self.on_paste_window_closed)

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

        print("copyU 已启动")
        print("=" * 40)
        print("快捷键说明:")
        print("  Ctrl+~     - 显示/隐藏剪贴板窗口")
        print("  Ctrl+C     - 复制（系统自动保存到历史）")
        print("  ↑/↓        - 选择历史记录")
        print("  Ctrl+1     - 粘贴纯文本")
        print("  Ctrl+Enter - 粘贴原格式(HTML)")
        print("  双击       - 直接粘贴原格式")
        print("  Esc        - 关闭窗口")
        print("=" * 40)

    def on_clipboard_changed(self):
        """系统剪贴板变化时自动保存（复用系统 Ctrl+C/V）"""
        # 获取剪贴板内容
        html_content, plain_text = self.clipboard_manager.get_content()

        if not plain_text and not html_content:
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
        print("Ctrl+~ 触发: 切换剪贴板窗口")

        if self.paste_window_visible:
            self.paste_window.hide()
            self.paste_window_visible = False
        else:
            # 加载记录并显示
            self.db_worker.add_task({
                'type': 'load',
                'limit': self.config.getint('UI', 'max_display_items', 50)
            })

    def show_paste_window(self):
        """显示粘贴窗口（从托盘菜单调用）"""
        self.toggle_paste_window()

    def on_records_loaded(self, records: List[Dict]):
        """记录加载完成"""
        self.paste_window.update_records(records)
        self.paste_window.show_at_cursor()
        self.paste_window_visible = True

    def on_paste_window_closed(self):
        """粘贴窗口关闭"""
        self.paste_window_visible = False

    def on_paste_requested(self, record_id: int, is_plain_text: bool):
        """处理粘贴请求"""
        # 查找对应记录
        for record in self.paste_window.records:
            if record['id'] == record_id:
                if is_plain_text:
                    self.clipboard_manager.set_content(plain_text=record['plain_text'])
                    print(f"粘贴纯文本 (ID: {record_id})")
                else:
                    self.clipboard_manager.set_content(
                        html=record['html_content'],
                        plain_text=record['plain_text']
                    )
                    print(f"粘贴HTML格式 (ID: {record_id})")

                # 模拟粘贴
                QTimer.singleShot(100, KeyboardSimulator.simulate_paste)
                break

    def on_record_saved(self, record_id: int):
        """记录保存成功"""
        print(f"剪贴板内容已保存 (ID: {record_id})")

    def trigger_cleanup(self):
        """触发清理任务"""
        print("触发过期记录清理")
        self.db_worker.add_task({'type': 'cleanup'})

    def on_cleanup_done(self, count: int):
        """清理完成"""
        if count > 0:
            print(f"已清理 {count} 条过期记录")

    def on_db_error(self, error_msg: str):
        """数据库错误"""
        print(f"数据库错误: {error_msg}")

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
            return super().exec_()
        finally:
            self.cleanup()

    def cleanup(self):
        """清理资源"""
        print("正在关闭 copyU...")
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

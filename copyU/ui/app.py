# -*- coding: utf-8 -*-
"""
主应用程序模块

copyU 主应用逻辑，协调各模块。
"""

import sys
import time
from typing import Dict, List, Tuple

from PyQt5.QtWidgets import QApplication, QMessageBox
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtNetwork import QLocalSocket, QLocalServer

from copyU.core.config import ConfigManager
from copyU.core.metrics import metrics
from copyU.core.rate_limiter import RateLimiter
from copyU.infrastructure.logging_config import logger
from copyU.infrastructure.database import DatabaseWorker
from copyU.infrastructure.thread_pool import ThreadPoolManager
from copyU.services.clipboard import ClipboardManager
from copyU.services.hotkey import GlobalHotkeyManager
from copyU.services.keyboard import KeyboardSimulator
from copyU.ui.paste_window import PasteWindow
from copyU.ui.tray_icon import TrayIcon


# 常量定义
MAX_CLIPBOARD_SIZE = 2_000_000  # 2MB 剪贴板内容大小限制


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
        record = None

        # 优先从当前选中的标签页查找记录
        current_tab_id = self.paste_window.current_tab_id
        if current_tab_id is not None and current_tab_id in self.paste_window.current_records:
            for r in self.paste_window.current_records[current_tab_id]:
                if r['id'] == record_id:
                    record = r
                    break

        # 如果当前标签页没有找到，再到其他标签页查找
        if record is None:
            for tab_id, records in self.paste_window.current_records.items():
                if tab_id == current_tab_id:
                    continue  # 跳过当前标签页（已经查找过）
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
            # 创建确认对话框 - 使用 None 作为父窗口（QApplication 不是 QWidget）
            self._cleanup_msg_box = QMessageBox(None)
            self._cleanup_msg_box.setWindowTitle('确认清理')
            self._cleanup_msg_box.setText('确定要清理所有默认页记录吗？\n\n注意：被固定到自定义标签页的记录将保留。')
            self._cleanup_msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            self._cleanup_msg_box.setDefaultButton(QMessageBox.No)
            self._cleanup_msg_box.setWindowFlag(Qt.WindowStaysOnTopHint, True)

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
        socket = QLocalSocket()
        socket.connectToServer("copyU_single_instance")
        if socket.waitForConnected(500):
            # 连接成功，说明已有实例在运行，发送激活信号
            logger.info("检测到已有实例在运行，发送激活信号...")
            socket.write(b"SHOW_WINDOW")
            socket.waitForBytesWritten(500)
            socket.close()
            return False
        socket.close()

        # 无法连接，创建服务器
        self._local_server = QLocalServer()
        self._local_server.listen("copyU_single_instance")
        # 监听新连接，处理来自其他实例的信号
        self._local_server.newConnection.connect(self._on_new_instance_connection)
        return True

    def _on_new_instance_connection(self):
        """处理新实例连接 - 其他实例尝试启动时触发"""
        socket = self._local_server.nextPendingConnection()
        if socket.waitForReadyRead(500):
            data = socket.readAll().data()
            if data == b"SHOW_WINDOW":
                logger.info("收到激活信号，显示主窗口")
                # 显示托盘图标（以防万一）
                if not self.tray_icon.isVisible():
                    self.tray_icon.show()
                # 显示粘贴窗口
                QTimer.singleShot(0, self.show_paste_window)
        socket.close()
        socket.deleteLater()

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

        # 清理单实例服务器
        if self._local_server:
            self._local_server.close()
            self._local_server = None
            logger.info("单实例服务器已关闭")


def main():
    """主函数"""
    # 设置高DPI支持
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = CopyUApp(sys.argv)
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()

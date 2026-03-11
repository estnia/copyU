# -*- coding: utf-8 -*-
"""
粘贴窗口模块

提供剪贴板历史窗口的UI，支持多标签页、拖拽排序等功能。
"""

import functools
from typing import Optional, Dict, List

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QLabel, QAbstractItemView, QTabWidget, QTabBar, QPushButton,
    QInputDialog, QMessageBox, QToolTip, QMenu, QAction, QLineEdit, QApplication
)
from PyQt5.QtCore import Qt, QTimer, QPoint, QRect, pyqtSignal
from PyQt5.QtGui import QFont, QColor, QKeyEvent, QCursor

from copyU.core.config import ConfigManager


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
        self._tab_cursor_positions = {}  # 保存光标位置

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

        # 获取当前标签页的列表控件 - 使用 currentWidget 确保与显示同步
        list_widget = self.tab_widget.currentWidget()
        if not list_widget or not isinstance(list_widget, QListWidget):
            return

        # 同步更新 current_tab_id 以确保其他逻辑正确
        current_index = self.tab_widget.currentIndex()
        if current_index >= 0:
            self.current_tab_id = self.get_tab_data(current_index)

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

            # 获取当前标签页ID（使用同步后的值或重新获取）
            current_index = self.tab_widget.currentIndex()
            current_tab_id = self.current_tab_id
            if current_index >= 0:
                current_tab_id = self.get_tab_data(current_index)

            # 获取当前标签页信息
            current_tab = next((t for t in self.tabs if t['id'] == current_tab_id), None)
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
                            self.unpin_record_requested.emit(record_id, current_tab_id)
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
                        self.unpin_record_requested.emit(record_id, current_tab_id)

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


# 导入 QApplication 用于 show_at_cursor
from PyQt5.QtWidgets import QApplication

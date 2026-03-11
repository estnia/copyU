# -*- coding: utf-8 -*-
"""
系统托盘图标模块

提供系统托盘图标和菜单功能。
"""

import os
import sys

from PyQt5.QtWidgets import QSystemTrayIcon, QMenu, QAction, QMessageBox, QApplication
from PyQt5.QtGui import QIcon, QColor, QPixmap, QPainter, QBrush
from PyQt5.QtCore import Qt

from copyU.infrastructure.logging_config import logger
from copyU.version import VERSION


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
            f'<h2>CopyU v{VERSION}</h2>'
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
        icon_path = os.path.join(script_dir, '..', '..', 'icon.svg')

        # 尝试加载SVG图标
        if os.path.exists(icon_path):
            self.setIcon(QIcon(icon_path))
        else:
            # 备用：生成简单的蓝色图标
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

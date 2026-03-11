# -*- coding: utf-8 -*-
"""
copyU 表现层模块

包含用户界面相关的组件：粘贴窗口、系统托盘、主应用。
"""

from .paste_window import PasteWindow
from .tray_icon import TrayIcon
from .app import CopyUApp, main

__all__ = ['PasteWindow', 'TrayIcon', 'CopyUApp', 'main']

# -*- coding: utf-8 -*-
"""
copyU - UOS V20 剪贴板管理工具
基于 PyQt5 + SQLite3 开发
"""

from .version import VERSION, __version__
from .ui import CopyUApp, main

__all__ = ['CopyUApp', 'main', 'VERSION', '__version__']

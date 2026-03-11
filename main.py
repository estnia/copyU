#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
copyU - UOS V20 剪贴板管理工具
基于 PyQt5 + SQLite3 开发

向后兼容的入口点 - 实际实现已移至 copyU 包
"""

import sys
import os

# 确保可以找到 copyU 包
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

from copyU.ui.app import main

if __name__ == '__main__':
    main()

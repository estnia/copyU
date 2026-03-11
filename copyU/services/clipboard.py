# -*- coding: utf-8 -*-
"""
剪贴板管理模块

提供剪贴板内容的读取和设置功能。
"""

from typing import Tuple

from PyQt5.QtCore import QMimeData
from PyQt5.QtGui import QClipboard


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

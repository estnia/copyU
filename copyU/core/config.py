# -*- coding: utf-8 -*-
"""
配置管理模块

使用 XDG 规范目录 ~/.config/copyu/ 管理应用配置
"""

import os
import configparser


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

    def set(self, section: str, key: str, value):
        """设置配置值"""
        if section not in self.config:
            self.config[section] = {}
        self.config[section][key] = str(value)

    def setint(self, section: str, key: str, value: int):
        """设置整数配置值"""
        self.set(section, key, value)

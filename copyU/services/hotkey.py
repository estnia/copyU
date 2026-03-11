# -*- coding: utf-8 -*-
"""
全局热键管理模块

提供系统级快捷键监听功能，支持 pynput 和 system_hotkey 两种后端。
"""

from copyU.infrastructure.logging_config import logger

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

from PyQt5.QtCore import QObject, pyqtSignal

from copyU.core.config import ConfigManager


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

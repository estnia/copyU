# -*- coding: utf-8 -*-
"""
键盘模拟模块

提供模拟键盘粘贴操作的功能，支持 xdotool 和 pyautogui 两种实现。
"""

import logging
import shutil

from PyQt5.QtCore import QObject, QProcess, QTimer, pyqtSignal


class KeyboardSimulator(QObject):
    """键盘模拟器，用于发送粘贴命令 - 使用异步 QProcess 避免阻塞主线程

    使用纯异步回调链实现，完全避免 QEventLoop 阻塞模式
    """

    _instance = None
    _xdotool_process = None
    _timer = None
    _finished_callback = None

    # 信号定义
    paste_completed = pyqtSignal(bool)  # 粘贴完成信号，参数表示成功/失败

    def __init__(self):
        super().__init__()
        self._logger = logging.getLogger('copyU.keyboard')
        self._init_process()

    @classmethod
    def get_instance(cls):
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = KeyboardSimulator()
        return cls._instance

    def _init_process(self):
        """初始化 QProcess"""
        if self._xdotool_process is None:
            self._xdotool_process = QProcess(self)
            self._xdotool_process.setProgram("/usr/bin/xdotool")
            self._xdotool_process.finished.connect(self._on_process_finished)

    def _on_process_finished(self, exit_code, exit_status):
        """QProcess 完成回调"""
        if self._timer:
            self._timer.stop()
            self._timer = None

        callback = self._finished_callback
        self._finished_callback = None

        if callback:
            success = (exit_code == 0 and exit_status == QProcess.NormalExit)
            if not success and exit_code != 0:
                stderr = self._xdotool_process.readAllStandardError().data().decode('utf-8', errors='ignore')
                if stderr:
                    self._logger.warning("xdotool 执行失败: %s", stderr)
            callback(success)

    def run_xdotool_async(self, args: list, callback, timeout_ms: int = 500):
        """异步运行 xdotool 命令

        Args:
            args: xdotool 参数列表
            callback: 完成回调函数，接收一个 bool 参数表示成功/失败
            timeout_ms: 超时时间（毫秒）
        """
        if not shutil.which("xdotool"):
            self._logger.warning("xdotool 未安装")
            callback(False)
            return

        try:
            # 确保进程不在运行中 - 使用异步方式终止
            if self._xdotool_process.state() != QProcess.NotRunning:
                self._xdotool_process.kill()
                # 异步等待，非阻塞，使用 QTimer 延迟启动新命令
                def delayed_start():
                    self._do_run_async(args, callback, timeout_ms)
                QTimer.singleShot(50, delayed_start)
            else:
                self._do_run_async(args, callback, timeout_ms)

        except (OSError, IOError, RuntimeError) as e:
            self._logger.warning("QProcess 执行失败: %s", e)
            self._finished_callback = None
            callback(False)

    def _do_run_async(self, args: list, callback, timeout_ms: int):
        """实际执行异步命令（内部方法）"""
        self._finished_callback = callback
        self._xdotool_process.setArguments(args)

        # 设置超时定时器
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._on_timeout(args))
        self._timer.start(timeout_ms)

        self._xdotool_process.start()

    def _on_timeout(self, args):
        """处理超时"""
        self._logger.warning("xdotool 执行超时: %s", args)
        try:
            self._xdotool_process.kill()
        except Exception:
            pass

        callback = self._finished_callback
        self._finished_callback = None
        if callback:
            callback(False)

    @classmethod
    def simulate_paste_async(cls, callback=None):
        """模拟 Ctrl+V 粘贴 - 完全异步非阻塞执行

        Args:
            callback: 可选的回调函数，接收 bool 参数表示成功/失败
        """
        logger = logging.getLogger('copyU.keyboard')
        simulator = cls.get_instance()

        def on_paste_done(success):
            """粘贴完成的统一回调"""
            if callback:
                callback(success)
            simulator.paste_completed.emit(success)

        # 优先使用 xdotool（Linux X11 环境）
        if shutil.which("xdotool"):
            # 使用 QTimer 延迟启动，确保用户已释放按键
            def start_keyup():
                def on_keyup_done(success):
                    if success:
                        # keyup 成功，继续执行 key
                        def do_key():
                            simulator.run_xdotool_async(
                                ["key", "ctrl+v"],
                                on_key_done,
                                timeout_ms=300
                            )

                        def on_key_done(success):
                            if success:
                                logger.debug("使用 xdotool 模拟粘贴成功")
                                on_paste_done(True)
                            else:
                                logger.warning("xdotool key 命令失败，尝试 pyautogui")
                                cls._fallback_to_pyautogui(on_paste_done)

                        QTimer.singleShot(20, do_key)  # 20ms 延迟
                    else:
                        logger.warning("xdotool keyup 命令失败，尝试 pyautogui")
                        cls._fallback_to_pyautogui(on_paste_done)

                simulator.run_xdotool_async(
                    ["keyup", "ctrl", "alt", "shift", "meta"],
                    on_keyup_done,
                    timeout_ms=300
                )

            QTimer.singleShot(50, start_keyup)  # 50ms 延迟确保按键释放
        else:
            logger.info("xdotool 未找到，尝试 pyautogui")
            cls._fallback_to_pyautogui(on_paste_done)

    @classmethod
    def _fallback_to_pyautogui(cls, callback):
        """回退到 pyautogui 实现 - 在后台线程中执行避免阻塞"""
        logger = logging.getLogger('copyU.keyboard')

        def run_pyautogui():
            try:
                import pyautogui
                import time
                pyautogui.PAUSE = 0.01
                pyautogui.keyUp('ctrl')
                pyautogui.keyUp('v')
                pyautogui.keyUp('alt')
                pyautogui.keyUp('shift')
                time.sleep(0.05)
                pyautogui.keyDown('ctrl')
                pyautogui.keyDown('v')
                pyautogui.keyUp('v')
                pyautogui.keyUp('ctrl')
                logger.debug("使用 pyautogui 模拟粘贴成功")
                return True
            except ImportError:
                logger.error("无法模拟键盘，请安装 xdotool 或 pyautogui")
                return False
            except Exception as e:
                logger.error("pyautogui 执行失败: %s", e)
                return False

        # 使用 QTimer 延迟执行，避免阻塞主事件循环
        def delayed_run():
            success = run_pyautogui()
            callback(success)

        QTimer.singleShot(50, delayed_run)

    @staticmethod
    def simulate_paste():
        """模拟 Ctrl+V 粘贴 - 兼容旧接口，内部调用异步版本

        注意：此静态方法已废弃，新代码应使用 simulate_paste_async
        """
        KeyboardSimulator.simulate_paste_async()

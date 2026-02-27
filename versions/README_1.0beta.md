# copyU 版本历史

## v1.0beta (2026-02-27)

### 功能特性
- **全局显示/隐藏**: `Ctrl+~` 切换剪贴板窗口显示
- **自动复制**: 监听系统 `Ctrl+C`，自动保存剪贴板历史
- **粘贴纯文本**: `Ctrl+1` 粘贴选中的纯文本内容
- **粘贴原格式**: `Ctrl+Enter` 或 `鼠标双击` 粘贴HTML格式内容
- **导航选择**: `↑/↓` 或 `鼠标单击` 选择历史记录
- **关闭窗口**: `Esc` 键关闭

### 技术实现
- 基于 PyQt5 + SQLite3 开发
- 使用 pynput/system_hotkey 实现全局热键
- 使用 xdotool/pyautogui 模拟键盘粘贴
- 支持 UOS V20 (Debian 10) 系统

### 文件清单
- `main_1.0beta.py` - 主程序文件
- `HOTKEY_CUSTOMIZATION_1.0beta.md` - 快捷键自定义指南

### 已知限制
- 需要安装依赖: `pynput`, `PyQt5`, `xdotool`(Linux)
- 首次运行会自动创建 `config.ini` 和 `clipboard_store.db`

# copyU v1.01 stable (2026-02-27)

## 修复问题

### 1. 粘贴偶尔输入"v"键
**原因**: xdotool `--clearmodifiers` 不可靠，修饰键状态混乱

**解决方案**:
- 先执行 `xdotool keyup ctrl alt shift meta` 强制释放所有修饰键
- 添加 20ms 延迟确保按键状态重置
- pyautogui 同样添加按键重置流程

### 2. "热键检测错误: grave"
**原因**: `Key.grave` 属性在部分系统不存在

**解决方案**:
- 使用 try/except 安全访问 `Key.grave`
- 过滤包含 "grave" 的错误日志

## 版本文件

| 文件 | 说明 |
|------|------|
| `main_v1.01_stable.py` | 主程序 (31.9 KB) |
| `HOTKEY_CUSTOMIZATION_v1.01.md` | 快捷键自定义指南 |

## 快捷键功能

| 快捷键 | 功能 |
|--------|------|
| `Ctrl+~` | 显示/隐藏剪贴板窗口 |
| `Ctrl+C` | 复制（自动保存到历史）|
| `Ctrl+1` | 粘贴纯文本 |
| `Ctrl+Enter` | 粘贴原格式(HTML) |
| `鼠标双击` | 直接粘贴原格式 |
| `↑/↓` | 上下选择历史记录 |
| `Esc` | 关闭窗口 |

## 历史版本

- v1.0beta (2026-02-27) - 初始测试版
- **v1.01 stable** - 修复粘贴和热键检测问题

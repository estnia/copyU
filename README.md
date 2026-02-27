# copyU - UOS V20 剪贴板管理工具

基于 PyQt5 + SQLite3 开发的轻量级剪贴板管理器，适用于 UOS V20 (Debian 10) 系统。

## 功能特性

- **独立存档** (`Alt+C`): 将剪贴板内容保存到本地数据库，不干扰系统剪贴板
- **选择性粘贴** (`Alt+V`): 弹出历史记录列表，支持 HTML/纯文本两种格式粘贴
- **自动清理**: 程序启动时及每隔1小时自动清理过期记录
- **内存控制**: 超过1MB的记录自动跳过
- **UOS深度风格**: 圆角、浅色调、无边框窗口

## 安装依赖

```bash
# 安装系统依赖
sudo apt update
sudo apt install -y python3-pyqt5 python3-pip xdotool

# 安装 Python 依赖
pip3 install -r requirements.txt
```

## 运行方式

```bash
python3 main.py
```

程序启动后会在系统托盘显示图标，右键可退出。

## 使用说明

| 快捷键 | 功能 |
|--------|------|
| `Alt+C` | 将当前剪贴板内容保存到历史记录 |
| `Alt+V` | 显示历史记录列表 |
| `↑/↓` | 在列表中上下选择 |
| `Enter` | 粘贴 HTML 格式（保留原格式） |
| `Shift+Enter` | 粘贴纯文本格式 |
| `Esc` | 关闭列表窗口 |

## 配置文件

`config.ini` 包含以下可配置项：

```ini
[general]
db_path = clipboard_store.db          # 数据库文件路径
cleanup_days = 3                      # 自动清理天数
cleanup_interval = 1                  # 清理间隔（小时）
max_record_size_mb = 1                # 单条记录最大大小

[hotkey]
copy_hotkey = <alt>+c                 # 存档快捷键
paste_hotkey = <alt>+v                # 粘贴快捷键

[ui]
opacity = 0.95                        # 窗口透明度
window_width = 400                    # 窗口宽度
window_height = 300                   # 窗口高度
```

## 数据存储

剪贴板历史记录保存在 `clipboard_store.db` (SQLite3 数据库)中，包含：
- HTML 内容（保留原格式）
- 纯文本内容
- 时间戳
- 应用程序名称（可选）

## 技术说明

- 全局热键：使用 `pynput` 或 `system_hotkey`
- 键盘模拟：使用 `xdotool` 或 `pyautogui`
- 数据库 I/O：使用 `QThread` 异步处理
- 界面风格：遵循 UOS 深度设计规范

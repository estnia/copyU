# copyU 快捷键自定义指南

## 默认快捷键配置

| 功能 | 快捷键 | 说明 |
|------|--------|------|
| 显示/隐藏剪贴板窗口 | `Ctrl + ~` | 全局热键，随时呼出/隐藏 |
| 复制 | `Ctrl + C` | 复用系统复制，自动保存到历史 |
| 粘贴纯文本 | `Ctrl + 1` | 选中项后按此键粘贴无格式文本 |
| 粘贴原格式 | `Ctrl + Enter` | 选中项后按此键粘贴HTML格式 |
| 粘贴原格式(快捷) | `鼠标双击` | 直接双击列表项粘贴原格式 |
| 选中项目 | `鼠标单击` 或 `↑↓` | 仅选中，不执行粘贴 |
| 关闭窗口 | `Esc` | 关闭剪贴板历史窗口 |

## 如何自定义快捷键

### 1. 修改全局热键 (显示/隐藏窗口)

编辑 `main.py` 第 586-589 行：

```python
# 定义热键: Ctrl+~ (grave 是反引号键)
hotkeys = {
    '<ctrl>+grave': on_hotkey_show,      # Ctrl+~ (默认)
    '<ctrl>+`': on_hotkey_show,          # 备用格式
    # 添加你的自定义热键，例如：
    # '<alt>+c': on_hotkey_show,         # Alt+C
    # '<ctrl>+<shift>+v': on_hotkey_show, # Ctrl+Shift+V
}
```

**支持的格式：**
- `<ctrl>+a` - Ctrl+A
- `<alt>+f` - Alt+F
- `<ctrl>+<alt>+t` - Ctrl+Alt+T
- `<ctrl>+<shift>+s` - Ctrl+Shift+S

### 2. 修改粘贴窗口内的快捷键

编辑 `PasteWindow.keyPressEvent` 方法 (main.py 第 465-526 行)：

```python
def keyPressEvent(self, event: QKeyEvent):
    key = event.key()
    modifiers = event.modifiers()

    if key == Qt.Key_Escape:
        self.hide()
        self.closed.emit()

    elif key == Qt.Key_1 and modifiers == Qt.ControlModifier:
        # Ctrl+1: 粘贴纯文本 (默认)
        # 修改为 Ctrl+2: key == Qt.Key_2
        # 修改为 Alt+1: modifiers == Qt.AltModifier
        ...

    elif key == Qt.Key_Return or key == Qt.Key_Enter:
        if modifiers == Qt.ControlModifier:
            # Ctrl+Enter: 粘贴原格式 (默认)
            # 修改为 Alt+Enter: modifiers == Qt.AltModifier
            ...
```

### 3. 修改双击行为

编辑 `PasteWindow.on_item_double_clicked` 方法 (main.py 第 459-463 行)：

```python
def on_item_double_clicked(self, item: QListWidgetItem):
    """项目双击事件 - 粘贴原格式（HTML）"""
    record_id = item.data(Qt.UserRole)
    self.paste_requested.emit(record_id, False)  # False = 原格式
    # 改为 True 则双击粘贴纯文本
    self.hide()
```

### 4. 修改鼠标单击行为

编辑 `PasteWindow.on_item_clicked` 方法 (main.py 第 454-457 行)：

```python
def on_item_clicked(self, item: QListWidgetItem):
    """项目单击事件 - 仅选中，不粘贴"""
    self.list_widget.setCurrentItem(item)
    # 如需改为单击即粘贴原格式，取消下面注释：
    # record_id = item.data(Qt.UserRole)
    # self.paste_requested.emit(record_id, False)
    # self.hide()
```

## 完整示例：改为 Alt+C / Alt+V 风格

如果你想把显示热键改为 Alt+C，粘贴纯文本改为 Alt+1：

### 步骤1：修改全局热键 (main.py 第 586-589 行)

```python
hotkeys = {
    '<alt>+c': on_hotkey_show,    # Alt+C 显示/隐藏
}
```

### 步骤2：修改粘贴窗口快捷键 (main.py 第 474-489 行)

```python
elif key == Qt.Key_1 and modifiers == Qt.AltModifier:  # Alt+1
    # 粘贴纯文本
    ...

elif key == Qt.Key_Return or key == Qt.Key_Enter:
    if modifiers == Qt.AltModifier:  # Alt+Enter
        # 粘贴原格式
        ...
```

### 步骤3：修改 system_hotkey 备用方案 (main.py 第 598 行)

```python
self.hotkey_manager.register(('alt', 'c'), callback=self.show_triggered.emit)
```

### 步骤4：修改停止时的注销 (main.py 第 609 行)

```python
self.hotkey_manager.unregister(('alt', 'c'))
```

## 常用 Qt 键值对照表

| 键名 | Qt 常量 |
|------|---------|
| 数字键 0-9 | `Qt.Key_0` - `Qt.Key_9` |
| 字母键 A-Z | `Qt.Key_A` - `Qt.Key_Z` |
| F1-F12 | `Qt.Key_F1` - `Qt.Key_F12` |
| 回车 | `Qt.Key_Return`, `Qt.Key_Enter` |
| Esc | `Qt.Key_Escape` |
| Tab | `Qt.Key_Tab` |
| 空格 | `Qt.Key_Space` |
| 上下左右 | `Qt.Key_Up`, `Qt.Key_Down`, `Qt.Key_Left`, `Qt.Key_Right` |
| Home/End | `Qt.Key_Home`, `Qt.Key_End` |
| PageUp/Dn | `Qt.Key_PageUp`, `Qt.Key_PageDown` |

## 修饰符对照表

| 修饰符 | Qt 常量 |
|--------|---------|
| Ctrl | `Qt.ControlModifier` |
| Alt | `Qt.AltModifier` |
| Shift | `Qt.ShiftModifier` |
| Meta (Win/Cmd) | `Qt.MetaModifier` |

## 注意事项

1. **全局热键冲突**：避免使用系统已占用的热键（如 Ctrl+C/V/X/Z）
2. **pynput 格式**：全局热键使用 `<ctrl>`, `<alt>`, `<shift>` 格式
3. **修改后重启**：修改快捷键后需要重启程序生效
4. **UOS 兼容性**：某些热键可能被 UOS 桌面环境占用，请测试确认

## 保存自定义配置到配置文件

如需将自定义热键保存到配置文件，修改 `ConfigManager.create_default_config` 方法：

```python
def create_default_config(self):
    """创建默认配置"""
    self.config['General'] = {
        'database_path': 'clipboard_store.db',
        'max_age_days': '3',
        'max_record_size_mb': '1',
        'cleanup_interval_hours': '1',
        'hotkey_show': '<alt>+c',  # 修改这里
        'hotkey_paste_plain': '<alt>+1',  # 添加新配置
        'hotkey_paste_html': '<alt>+return',  # 添加新配置
    }
    ...
```

然后在代码中读取这些配置值来动态设置热键。

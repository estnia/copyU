# copyU v1.0.6 Stable

发布日期: 2026-02-27

## 修复问题

1. **配置文件和数据库存储位置**
   - 从 `~/` 移至 `~/.config/copyu/`
   - 符合 XDG Base Directory 规范

2. **任务栏托盘图标**
   - 使用设计的剪刀+U图标 (icon.svg)
   - 替换原来的蓝色圆圈图标

3. **单实例运行**
   - 使用 QLocalSocket/Server 实现单实例检测
   - 重复点击开始菜单不会启动多个实例

4. **窗口失焦自动隐藏**
   - 窗口失去焦点时自动隐藏
   - 点击窗口外部区域自动隐藏

5. **开始菜单图标**
   - 支持小开始菜单显示图标（通过SVG图标）

## 文件清单

- `copyu_1.0.6_amd64.deb` - Debian 安装包
- `main.py` - 主程序源码
- `icon.svg` - 应用程序图标

## 安装方法

```bash
sudo dpkg -i copyu_1.0.6_amd64.deb
```

## 配置说明

配置文件位置: `~/.config/copyu/config.ini`
数据库位置: `~/.config/copyu/clipboard_store.db`

#!/bin/bash
# copyU 卸载脚本

set -e

APP_DIR="/opt/copyU"
DESKTOP_FILE="/usr/share/applications/copyU.desktop"

echo "=== copyU 卸载程序 ==="
echo

if [ "$EUID" -ne 0 ]; then
    echo "请使用 sudo 运行此脚本"
    exit 1
fi

echo "[1/2] 移除应用文件..."
if [ -d "$APP_DIR" ]; then
    rm -rf "$APP_DIR"
    echo "已删除 $APP_DIR"
else
    echo "应用目录不存在"
fi

echo "[2/2] 移除桌面入口..."
if [ -f "$DESKTOP_FILE" ]; then
    rm -f "$DESKTOP_FILE"
    echo "已删除 $DESKTOP_FILE"
fi

# 更新桌面数据库
update-desktop-database /usr/share/applications/ 2>/dev/null || true

echo
echo "=== 卸载完成 ==="

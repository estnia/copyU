#!/bin/bash
# copyU 安装脚本 for Linux/UOS

set -e

APP_NAME="copyU"
APP_DIR="/opt/copyU"
DESKTOP_FILE="/usr/share/applications/copyU.desktop"
ICON_SIZE=64

echo "=== copyU 安装程序 ==="
echo

# 检查root权限
if [ "$EUID" -ne 0 ]; then
    echo "请使用 sudo 运行此脚本"
    echo "示例: sudo ./install.sh"
    exit 1
fi

# 检查依赖
echo "[1/4] 检查依赖..."
MISSING_DEPS=""

if ! command -v python3 &> /dev/null; then
    MISSING_DEPS="$MISSING_DEPS python3"
fi

if ! python3 -c "import PyQt5" 2>/dev/null; then
    MISSING_DEPS="$MISSING_DEPS python3-pyqt5"
fi

if ! python3 -c "import pynput" 2>/dev/null; then
    MISSING_DEPS="$MISSING_DEPS python3-pynput"
fi

if ! command -v xdotool &> /dev/null; then
    MISSING_DEPS="$MISSING_DEPS xdotool"
fi

if [ -n "$MISSING_DEPS" ]; then
    echo "缺少以下依赖，正在安装:$MISSING_DEPS"
    apt-get update
    apt-get install -y$MISSING_DEPS
fi

echo "依赖检查完成"
echo

# 创建应用目录
echo "[2/4] 创建应用目录..."
mkdir -p "$APP_DIR"
cp -r . "$APP_DIR/"
echo "应用已安装到 $APP_DIR"
echo

# 创建图标
echo "[3/4] 创建图标..."
ICON_PATH="$APP_DIR/icon.png"
python3 << EOF
from PyQt5.QtGui import QPixmap, QPainter, QBrush, QColor, QFont
from PyQt5.QtCore import Qt

pixmap = QPixmap($ICON_SIZE, $ICON_SIZE)
pixmap.fill(Qt.transparent)

painter = QPainter(pixmap)
painter.setRenderHint(QPainter.Antialiasing)

# 绘制蓝色圆形背景
painter.setBrush(QBrush(QColor(0, 120, 215)))
painter.setPen(Qt.NoPen)
painter.drawEllipse(2, 2, $ICON_SIZE-4, $ICON_SIZE-4)

# 绘制白色文字"U"
painter.setPen(QColor(255, 255, 255))
font = QFont("Noto Sans CJK SC", 28, QFont.Bold)
painter.setFont(font)
painter.drawText(pixmap.rect(), Qt.AlignCenter, "U")

painter.end()
pixmap.save("$ICON_PATH")
print(f"图标已创建: $ICON_PATH")
EOF

echo

# 创建桌面文件
echo "[4/4] 创建桌面入口..."
cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Name=copyU
Name[zh_CN]=copyU剪贴板
Comment=UOS V20 剪贴板管理工具
Comment[zh_CN]=UOS V20 剪贴板管理工具
Exec=python3 $APP_DIR/main.py
Icon=$ICON_PATH
Type=Application
Terminal=false
Categories=Utility;Office;
StartupNotify=true
StartupWMClass=copyU
Keywords=clipboard;copy;paste;剪贴板;
EOF

chmod +x "$DESKTOP_FILE"

echo "桌面入口已创建: $DESKTOP_FILE"
echo

# 更新桌面数据库
update-desktop-database /usr/share/applications/ 2>/dev/null || true

echo "=== 安装完成 ==="
echo
echo "启动方式:"
echo "  1. 从应用菜单搜索 'copyU'"
echo "  2. 运行命令: python3 $APP_DIR/main.py"
echo
echo "卸载方法: sudo ./uninstall.sh"

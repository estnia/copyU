#!/bin/bash
#
# CopyU deb 包构建脚本
# 自动同步 main.py 并构建 deb 包
#

set -e

VERSION="1.3.2"
DEB_NAME="copyu_${VERSION}_offline_amd64.deb"
BUILD_DIR="deb_build/copyu"

echo "========================================"
echo "   CopyU deb 包构建脚本"
echo "   版本: $VERSION"
echo "========================================"
echo ""

# 检查必要文件
if [ ! -f "main.py" ]; then
    echo "错误: 未找到 main.py"
    exit 1
fi

# 确保 postinst 有执行权限
chmod +x ${BUILD_DIR}/DEBIAN/postinst

# 同步 main.py 到构建目录
echo "[1/3] 同步 main.py..."
cp main.py ${BUILD_DIR}/opt/copyu/main.py
echo "  ✓ main.py 已复制"

# 检查 wheels 目录
echo ""
echo "[2/3] 检查 wheels..."
WHEEL_COUNT=$(ls ${BUILD_DIR}/opt/copyu/wheels/*.whl 2>/dev/null | wc -l)
echo "  ✓ 找到 $WHEEL_COUNT 个 wheel 文件"

# 构建 deb 包
echo ""
echo "[3/3] 构建 deb 包..."
dpkg-deb --build -Zgzip ${BUILD_DIR} ${DEB_NAME}

# 检查构建结果
if [ -f "${DEB_NAME}" ]; then
    SIZE=$(du -h ${DEB_NAME} | cut -f1)
    echo "  ✓ 构建成功: ${DEB_NAME} (${SIZE})"
else
    echo "  ✗ 构建失败"
    exit 1
fi

# 验证包信息
echo ""
echo "========================================"
echo "   包信息"
echo "========================================"
dpkg-deb -I ${DEB_NAME} | grep -E "Package|Version|Architecture|Depends"
echo ""
echo "✓ 构建完成！"
echo "  文件: ${DEB_NAME}"

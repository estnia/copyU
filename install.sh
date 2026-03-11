#!/bin/bash
#
# CopyU 一键安装脚本
# 自动处理 dpkg 安装和 apt 依赖修复
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEB_FILE="copyu_1.3.2_offline_amd64.deb"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查 root 权限
check_root() {
    if [ "$EUID" -ne 0 ]; then
        log_error "请使用 sudo 运行此脚本"
        echo "用法: sudo ./install.sh"
        exit 1
    fi
}

# 检查 deb 文件是否存在
check_deb() {
    if [ ! -f "$SCRIPT_DIR/$DEB_FILE" ]; then
        log_error "未找到 $DEB_FILE"
        echo "请确保此脚本与 deb 包在同一目录"
        exit 1
    fi
}

# 安装 deb 包
install_deb() {
    log_info "正在安装 CopyU..."

    # 尝试安装 deb 包
    if dpkg -i "$SCRIPT_DIR/$DEB_FILE" 2>/dev/null; then
        log_info "deb 包安装成功"
    else
        log_warn "deb 包安装需要依赖修复"

        # 检查网络连接
        if ping -c 1 -W 3 archive.ubuntu.com &> /dev/null || \
           ping -c 1 -W 3 mirrors.aliyun.com &> /dev/null || \
           ping -c 1 -W 3 uos.packages.deepin.com &> /dev/null; then
            log_info "检测到网络连接，正在自动修复依赖..."
            apt-get update
            apt-get install -f -y
        else
            log_warn "无网络连接，请手动修复依赖:"
            echo "  sudo apt-get install -f"
            exit 1
        fi
    fi
}

# 配置完成
finish() {
    echo ""
    log_info "CopyU 安装完成！"
    echo ""
    echo "使用方法:"
    echo "  1. 启动: 在应用菜单中找到 CopyU 或运行 copyu 命令"
    echo "  2. 热键: Ctrl+\` (反引号键) 显示/隐藏窗口"
    echo "  3. 粘贴: Ctrl+1~9 快速粘贴历史记录"
    echo "  4. 托盘: 右键托盘图标可退出或访问设置"
    echo ""
    echo "注意: 首次启动可能需要几秒钟加载"
}

# 主流程
main() {
    echo "========================================"
    echo "   CopyU 一键安装脚本"
    echo "   版本: 1.3.2 (离线版)"
    echo "========================================"
    echo ""

    check_root
    check_deb
    install_deb
    finish
}

main "$@"

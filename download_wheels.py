#!/usr/bin/env python3
"""
CopyU 离线依赖下载脚本
用于下载 pyautogui、pynput 及其依赖的 wheels 文件
"""

import os
import sys
import subprocess
import platform
import argparse


# 需要下载的依赖列表
REQUIRED_PACKAGES = [
    "pynput",
    "pyautogui",
    "pyscreeze",
    "pymsgbox",
    "pytweening",
    "mouseinfo",
    "pillow",
    "six",
    "setuptools",
    "python-xlib",
]


def get_python_version():
    """获取 Python 版本 (如 3.7, 3.8, 3.9)"""
    major = sys.version_info.major
    minor = sys.version_info.minor
    return f"{major}.{minor}", f"cp{major}{minor}"


def get_platform():
    """获取平台信息"""
    machine = platform.machine().lower()
    system = platform.system().lower()

    # 架构映射
    arch_map = {
        'x86_64': 'x86_64',
        'amd64': 'x86_64',
        'aarch64': 'aarch64',
        'arm64': 'aarch64',
    }

    arch = arch_map.get(machine, machine)

    if system == 'linux':
        return f"manylinux_2_17_{arch}"
    elif system == 'darwin':
        return f"macosx_10_9_{arch}"
    elif system == 'windows':
        return f"win_{arch}"
    return None


def download_wheels(output_dir, python_version=None, platform=None, pure_python_only=False):
    """
    下载 wheels 文件

    Args:
        output_dir: 输出目录
        python_version: Python 版本 (如 3.8)，None 则使用当前版本
        platform: 目标平台，None 则使用当前平台
        pure_python_only: 是否只下载纯 Python 包（无平台限制）
    """
    os.makedirs(output_dir, exist_ok=True)

    # 获取 Python 版本
    py_ver, cp_ver = get_python_version()
    if python_version:
        py_ver = python_version
        cp_ver = f"cp{python_version.replace('.', '')}"

    # 获取平台
    target_platform = platform or get_platform()

    print(f"=" * 60)
    print(f"CopyU 离线依赖下载工具")
    print(f"=" * 60)
    print(f"Python 版本: {py_ver}")
    print(f"目标平台: {target_platform or 'any (纯 Python)'}")
    print(f"输出目录: {output_dir}")
    print(f"=" * 60)

    failed_packages = []

    for package in REQUIRED_PACKAGES:
        print(f"\n正在下载: {package}")

        cmd = [
            sys.executable, "-m", "pip", "download",
            package,
            "-d", output_dir,
            "--only-binary=:all:" if not pure_python_only else "",
        ]

        # 添加平台和 Python 版本限制（如果不是纯 Python 模式）
        if not pure_python_only and target_platform:
            cmd.extend([
                "--platform", target_platform,
                "--python-version", py_ver,
            ])

        # 移除空字符串
        cmd = [c for c in cmd if c]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300
            )

            if result.returncode == 0:
                print(f"  ✓ {package} 下载成功")
            else:
                # 如果带平台限制失败，尝试不限制平台
                if not pure_python_only:
                    print(f"  ⚠ 带平台限制下载失败，尝试纯 Python 模式...")
                    cmd_fallback = [
                        sys.executable, "-m", "pip", "download",
                        package,
                        "-d", output_dir,
                    ]
                    result2 = subprocess.run(
                        cmd_fallback,
                        capture_output=True,
                        text=True,
                        timeout=300
                    )
                    if result2.returncode == 0:
                        print(f"  ✓ {package} 下载成功（纯 Python）")
                    else:
                        print(f"  ✗ {package} 下载失败")
                        failed_packages.append(package)
                else:
                    print(f"  ✗ {package} 下载失败")
                    failed_packages.append(package)

        except subprocess.TimeoutExpired:
            print(f"  ✗ {package} 下载超时")
            failed_packages.append(package)
        except Exception as e:
            print(f"  ✗ {package} 下载出错: {e}")
            failed_packages.append(package)

    # 下载 evdev（Linux 专用）
    if platform is None or 'linux' in (platform or '').lower():
        print(f"\n正在下载: evdev (Linux 专用)")
        try:
            cmd = [
                sys.executable, "-m", "pip", "download",
                "evdev",
                "-d", output_dir,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                print(f"  ✓ evdev 下载成功")
            else:
                print(f"  ⚠ evdev 下载失败（可能非 Linux 系统）")
        except Exception as e:
            print(f"  ⚠ evdev 下载出错: {e}")

    # 显示结果
    print(f"\n" + "=" * 60)
    print(f"下载完成!")
    print(f"=" * 60)

    # 列出下载的文件
    print(f"\n已下载的文件:")
    downloaded_files = sorted(os.listdir(output_dir))
    total_size = 0
    for f in downloaded_files:
        filepath = os.path.join(output_dir, f)
        size = os.path.getsize(filepath)
        total_size += size
        size_str = f"{size / 1024 / 1024:.2f} MB" if size > 1024 * 1024 else f"{size / 1024:.2f} KB"
        print(f"  - {f} ({size_str})")

    print(f"\n总计: {len(downloaded_files)} 个文件, {total_size / 1024 / 1024:.2f} MB")

    if failed_packages:
        print(f"\n⚠ 以下包下载失败:")
        for pkg in failed_packages:
            print(f"  - {pkg}")
        return 1

    print(f"\n✓ 所有依赖下载成功!")
    print(f"\n下一步:")
    print(f"  1. 将 '{output_dir}' 目录中的文件上传到:")
    print(f"     deb_build/copyu/opt/copyu/wheels/")
    print(f"  2. 运行: dpkg-deb --build -Zgzip deb_build/copyu copyu_x.x.x_amd64.deb")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="下载 CopyU 的离线依赖 wheels",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 下载到默认目录 (wheels_output/)
  python3 download_wheels.py

  # 指定输出目录
  python3 download_wheels.py -o ./my_wheels

  # 指定 Python 版本和平台（用于为其他环境打包）
  python3 download_wheels.py --py 3.8 --platform manylinux_2_17_x86_64

  # 只下载纯 Python 包（跨平台兼容）
  python3 download_wheels.py --pure-python
        """
    )

    parser.add_argument(
        "-o", "--output",
        default="wheels_output",
        help="输出目录 (默认: wheels_output/)"
    )

    parser.add_argument(
        "--py", "--python-version",
        help="目标 Python 版本 (如 3.7, 3.8, 3.9, 3.10, 3.11)"
    )

    parser.add_argument(
        "--platform",
        help="目标平台 (如 manylinux_2_17_x86_64, manylinux_2_17_aarch64)"
    )

    parser.add_argument(
        "--pure-python",
        action="store_true",
        help="只下载纯 Python 包（无平台限制，兼容性最好）"
    )

    args = parser.parse_args()

    return download_wheels(
        output_dir=args.output,
        python_version=args.py,
        platform=args.platform,
        pure_python_only=args.pure_python
    )


if __name__ == "__main__":
    sys.exit(main())

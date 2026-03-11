#!/usr/bin/env python3
"""
生成多尺寸PNG图标用于UOS开始菜单
使用 PyQt5 的 QSvgRenderer 渲染 SVG
"""
import os
import sys

from PyQt5.QtCore import Qt, QSize
from PyQt5.QtGui import QPixmap, QPainter
from PyQt5.QtWidgets import QApplication

# 需要先创建QApplication
app = QApplication.instance() or QApplication(sys.argv)

# 图标尺寸
ICON_SIZES = [16, 24, 32, 48, 64, 128, 256]

def generate_icons():
    """使用 PyQt5 生成PNG图标"""
    from PyQt5.QtSvg import QSvgRenderer

    script_dir = os.path.dirname(os.path.abspath(__file__))
    svg_path = os.path.join(script_dir, 'icon.svg')

    if not os.path.exists(svg_path):
        print(f"错误: 找不到图标文件 {svg_path}")
        sys.exit(1)

    # 加载SVG
    renderer = QSvgRenderer(svg_path)
    if not renderer.isValid():
        print("错误: 无法加载SVG文件")
        sys.exit(1)

    # 创建图标目录
    icons_dir = os.path.join(script_dir, 'icons')
    os.makedirs(icons_dir, exist_ok=True)

    # 生成各尺寸图标
    for size in ICON_SIZES:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        renderer.render(painter)
        painter.end()

        png_path = os.path.join(icons_dir, f'icon_{size}x{size}.png')
        if pixmap.save(png_path, 'PNG'):
            print(f"已生成: {png_path}")
        else:
            print(f"保存 {size}x{size} 图标失败")

    # 生成 hicolor 目录结构
    hicolor_dir = os.path.join(script_dir, 'hicolor')
    for size in ICON_SIZES:
        size_dir = os.path.join(hicolor_dir, f'{size}x{size}', 'apps')
        os.makedirs(size_dir, exist_ok=True)

        png_path = os.path.join(icons_dir, f'icon_{size}x{size}.png')
        target_path = os.path.join(size_dir, 'copyu.png')

        if os.path.exists(png_path):
            import shutil
            shutil.copy2(png_path, target_path)
            print(f"已复制到: {target_path}")

    # 同时生成主目录的 icon.png
    main_icon = os.path.join(script_dir, 'icon.png')
    if os.path.exists(os.path.join(icons_dir, 'icon_64x64.png')):
        import shutil
        shutil.copy2(os.path.join(icons_dir, 'icon_64x64.png'), main_icon)
        print(f"已生成主图标: {main_icon}")

    print("\n图标生成完成!")
    print(f"图标目录: {icons_dir}")
    print(f"hicolor目录: {hicolor_dir}")

if __name__ == '__main__':
    generate_icons()

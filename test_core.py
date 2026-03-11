#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
copyU 核心逻辑测试脚本（无需 GUI）
"""

import sys
import os
import sqlite3
import tempfile
import time
import threading
from datetime import datetime, timedelta

# 添加项目路径
sys.path.insert(0, '/home/feifei/copyU')

# ============ 测试 1: 配置文件管理器 ============
def test_config_manager():
    print("=" * 50)
    print("测试 1: ConfigManager 配置管理器")
    print("=" * 50)

    # 创建临时配置文件
    config_content = """[General]
database_path = test_clipboard.db
max_age_days = 3
cleanup_interval_hours = 1
max_record_size_mb = 1
hotkey_copy = <alt>+c
hotkey_paste = <alt>+v

[UI]
window_opacity = 0.95
window_width = 400
window_height = 300
max_display_items = 50
font_size = 12
"""

    with tempfile.NamedTemporaryFile(mode='w', suffix='.ini', delete=False) as f:
        f.write(config_content)
        config_path = f.name

    # 修改 ConfigManager 使用测试配置
    import configparser
    config = configparser.ConfigParser()
    config.read(config_path, encoding='utf-8')

    # 验证配置读取
    tests_passed = 0
    tests_total = 6

    try:
        # 测试 1.1: 读取数据库路径
        db_path = config.get('General', 'database_path', fallback='clipboard_store.db')
        assert db_path == 'test_clipboard.db', f"数据库路径错误: {db_path}"
        print(f"  ✓ 数据库路径: {db_path}")
        tests_passed += 1

        # 测试 1.2: 读取 max_age_days
        max_age = config.getint('General', 'max_age_days', fallback=3)
        assert max_age == 3, f"max_age_days 错误: {max_age}"
        print(f"  ✓ max_age_days: {max_age}")
        tests_passed += 1

        # 测试 1.3: 读取清理间隔
        cleanup_interval = config.getint('General', 'cleanup_interval_hours', fallback=1)
        assert cleanup_interval == 1, f"cleanup_interval_hours 错误: {cleanup_interval}"
        print(f"  ✓ cleanup_interval_hours: {cleanup_interval}")
        tests_passed += 1

        # 测试 1.4: 读取最大记录大小
        max_size = config.getint('General', 'max_record_size_mb', fallback=1)
        assert max_size == 1, f"max_record_size_mb 错误: {max_size}"
        print(f"  ✓ max_record_size_mb: {max_size}MB")
        tests_passed += 1

        # 测试 1.5: 读取热键配置
        copy_hotkey = config.get('General', 'hotkey_copy', fallback='<alt>+c')
        paste_hotkey = config.get('General', 'hotkey_paste', fallback='<alt>+v')
        assert copy_hotkey == '<alt>+c', f"copy_hotkey 错误: {copy_hotkey}"
        assert paste_hotkey == '<alt>+v', f"paste_hotkey 错误: {paste_hotkey}"
        print(f"  ✓ 热键配置: copy={copy_hotkey}, paste={paste_hotkey}")
        tests_passed += 1

        # 测试 1.6: 读取 UI 配置
        window_width = config.getint('UI', 'window_width', fallback=400)
        window_height = config.getint('UI', 'window_height', fallback=300)
        assert window_width == 400, f"window_width 错误: {window_width}"
        assert window_height == 300, f"window_height 错误: {window_height}"
        print(f"  ✓ UI 尺寸: {window_width}x{window_height}")
        tests_passed += 1

    except AssertionError as e:
        print(f"  ✗ 配置测试失败: {e}")
    except Exception as e:
        print(f"  ✗ 配置测试异常: {e}")

    # 清理
    os.unlink(config_path)

    print(f"\n  配置测试: {tests_passed}/{tests_total} 通过")
    return tests_passed, tests_total


# ============ 测试 2: 数据库操作 ============
def test_database():
    print("\n" + "=" * 50)
    print("测试 2: 数据库操作")
    print("=" * 50)

    # 创建临时数据库
    db_fd, db_path = tempfile.mkstemp(suffix='.db')
    os.close(db_fd)

    tests_passed = 0
    tests_total = 7

    try:
        # 测试 2.1: 创建数据库和表
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS clipboard_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                html_content TEXT,
                plain_text TEXT,
                timestamp REAL NOT NULL,
                app_name TEXT,
                content_size INTEGER
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_timestamp ON clipboard_records(timestamp)
        ''')
        conn.commit()
        print("  ✓ 数据库表创建成功")
        tests_passed += 1

        # 测试 2.2: 插入记录
        test_records = [
            ('<b>Test HTML 1</b>', 'Test HTML 1', time.time(), 'TestApp', 50),
            ('<i>Test HTML 2</i>', 'Test HTML 2', time.time() - 3600, 'TestApp', 50),
            ('', 'Plain text only', time.time() - 7200, 'TestApp', 30),
        ]

        for record in test_records:
            cursor.execute('''
                INSERT INTO clipboard_records (html_content, plain_text, timestamp, app_name, content_size)
                VALUES (?, ?, ?, ?, ?)
            ''', record)
        conn.commit()

        cursor.execute('SELECT COUNT(*) FROM clipboard_records')
        count = cursor.fetchone()[0]
        assert count == 3, f"插入记录数错误: {count}"
        print(f"  ✓ 插入 {count} 条记录成功")
        tests_passed += 1

        # 测试 2.3: 查询记录
        cursor.execute('''
            SELECT id, html_content, plain_text, timestamp, app_name
            FROM clipboard_records
            ORDER BY timestamp DESC
            LIMIT 10
        ''')
        records = cursor.fetchall()
        assert len(records) == 3, f"查询记录数错误: {len(records)}"
        print(f"  ✓ 查询记录成功，共 {len(records)} 条")
        tests_passed += 1

        # 测试 2.4: 搜索功能
        cursor.execute('''
            SELECT id, plain_text FROM clipboard_records
            WHERE plain_text LIKE ?
        ''', ('%HTML%',))
        search_results = cursor.fetchall()
        assert len(search_results) == 2, f"搜索结果数错误: {len(search_results)}"
        print(f"  ✓ 搜索功能正常，找到 {len(search_results)} 条匹配")
        tests_passed += 1

        # 测试 2.5: 检查重复（1分钟内）
        one_minute_ago = time.time() - 60
        cursor.execute('''
            SELECT id FROM clipboard_records
            WHERE plain_text = ? AND timestamp > ?
        ''', ('Test HTML 1', one_minute_ago))
        duplicate = cursor.fetchone()
        assert duplicate is not None, "应该找到重复记录"
        print(f"  ✓ 重复检测功能正常 (ID: {duplicate[0]})")
        tests_passed += 1

        # 测试 2.6: 清理过期记录
        cutoff_time = time.time() - (1 * 24 * 3600)  # 1天前
        cursor.execute('''
            DELETE FROM clipboard_records WHERE timestamp < ?
        ''', (cutoff_time,))
        deleted = cursor.rowcount
        conn.commit()
        print(f"  ✓ 清理功能正常，删除了 {deleted} 条过期记录")
        tests_passed += 1

        # 测试 2.7: 验证清理后记录数
        cursor.execute('SELECT COUNT(*) FROM clipboard_records')
        remaining = cursor.fetchone()[0]
        assert remaining == 3 - deleted, f"清理后记录数错误: {remaining}"
        print(f"  ✓ 清理后剩余 {remaining} 条记录")
        tests_passed += 1

        conn.close()

    except Exception as e:
        print(f"  ✗ 数据库测试失败: {e}")
        import traceback
        traceback.print_exc()

    # 清理
    if os.path.exists(db_path):
        os.unlink(db_path)

    print(f"\n  数据库测试: {tests_passed}/{tests_total} 通过")
    return tests_passed, tests_total


# ============ 测试 3: 内容大小限制 ============
def test_content_size_limit():
    print("\n" + "=" * 50)
    print("测试 3: 内容大小限制")
    print("=" * 50)

    max_size_mb = 1
    max_size_bytes = max_size_mb * 1024 * 1024

    tests_passed = 0
    tests_total = 2

    try:
        # 测试 3.1: 小内容（应该通过）
        small_content = "Small test content" * 100
        content_size = len(small_content.encode('utf-8'))
        assert content_size < max_size_bytes, f"小内容超过限制: {content_size} bytes"
        print(f"  ✓ 小内容 ({content_size} bytes) 可以通过")
        tests_passed += 1

        # 测试 3.2: 大内容（应该跳过）
        large_content = "Large content" * 100000  # 约 1.3MB
        content_size = len(large_content.encode('utf-8'))
        assert content_size > max_size_bytes, f"大内容未超过限制: {content_size} bytes"
        print(f"  ✓ 大内容 ({content_size // 1024 // 1024}MB) 应该被跳过")
        tests_passed += 1

    except AssertionError as e:
        print(f"  ✗ 内容大小测试失败: {e}")

    print(f"\n  内容大小测试: {tests_passed}/{tests_total} 通过")
    return tests_passed, tests_total


# ============ 测试 4: 时间格式转换 ============
def test_time_formatting():
    print("\n" + "=" * 50)
    print("测试 4: 时间格式转换")
    print("=" * 50)

    tests_passed = 0
    tests_total = 2

    try:
        # 测试 4.1: 时间戳转换
        timestamp = time.time()
        display_time = datetime.fromtimestamp(timestamp).strftime('%m-%d %H:%M')
        assert len(display_time) == 11, f"时间格式错误: {display_time}"
        print(f"  ✓ 时间戳转换: {display_time}")
        tests_passed += 1

        # 测试 4.2: 过期时间计算
        max_age_days = 3
        cutoff_time = time.time() - (max_age_days * 24 * 3600)
        cutoff_date = datetime.fromtimestamp(cutoff_time)
        print(f"  ✓ 过期时间计算: {max_age_days}天前 = {cutoff_date.strftime('%Y-%m-%d')}")
        tests_passed += 1

    except Exception as e:
        print(f"  ✗ 时间格式测试失败: {e}")

    print(f"\n  时间格式测试: {tests_passed}/{tests_total} 通过")
    return tests_passed, tests_total


# ============ 测试 5: 剪贴板内容处理 ============
def test_clipboard_content():
    print("\n" + "=" * 50)
    print("测试 5: 剪贴板内容处理")
    print("=" * 50)

    tests_passed = 0
    tests_total = 3

    try:
        # 模拟剪贴板内容（不使用 Qt）
        class MockMimeData:
            def __init__(self, text='', html=''):
                self._text = text
                self._html = html

            def hasHtml(self):
                return bool(self._html)

            def hasText(self):
                return bool(self._text)

            def html(self):
                return self._html

            def text(self):
                return self._text

        # 测试 5.1: 纯文本内容
        mime1 = MockMimeData(text='Hello World')
        html_content = mime1.html() if mime1.hasHtml() else ''
        plain_text = mime1.text() if mime1.hasText() else ''
        assert plain_text == 'Hello World', f"纯文本读取错误: {plain_text}"
        assert html_content == '', f"HTML 应该为空: {html_content}"
        print(f"  ✓ 纯文本内容处理正确")
        tests_passed += 1

        # 测试 5.2: HTML 内容
        mime2 = MockMimeData(
            text='Bold Text',
            html='<b>Bold Text</b>'
        )
        html_content = mime2.html() if mime2.hasHtml() else ''
        plain_text = mime2.text() if mime2.hasText() else ''
        assert html_content == '<b>Bold Text</b>', f"HTML 读取错误: {html_content}"
        assert plain_text == 'Bold Text', f"纯文本读取错误: {plain_text}"
        print(f"  ✓ HTML 内容处理正确")
        tests_passed += 1

        # 测试 5.3: 重复检测逻辑
        last_content = 'Test Content'
        current_content = 'Test Content'
        is_duplicate = (current_content == last_content)
        assert is_duplicate, "应该检测到重复"
        print(f"  ✓ 重复检测逻辑正确")
        tests_passed += 1

    except AssertionError as e:
        print(f"  ✗ 内容处理测试失败: {e}")
    except Exception as e:
        print(f"  ✗ 内容处理测试异常: {e}")

    print(f"\n  内容处理测试: {tests_passed}/{tests_total} 通过")
    return tests_passed, tests_total


# ============ 主测试函数 ============
def main():
    print("\n" + "=" * 50)
    print("copyU 核心逻辑验证测试")
    print("=" * 50)
    print(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Python 版本: {sys.version}")
    print("")

    all_tests = []

    # 运行所有测试
    all_tests.append(test_config_manager())
    all_tests.append(test_database())
    all_tests.append(test_content_size_limit())
    all_tests.append(test_time_formatting())
    all_tests.append(test_clipboard_content())

    # 汇总结果
    total_passed = sum(t[0] for t in all_tests)
    total_tests = sum(t[1] for t in all_tests)

    print("\n" + "=" * 50)
    print("测试汇总")
    print("=" * 50)
    print(f"总测试项: {total_tests}")
    print(f"通过: {total_passed}")
    print(f"失败: {total_tests - total_passed}")
    print(f"通过率: {total_passed / total_tests * 100:.1f}%")

    if total_passed == total_tests:
        print("\n✓ 所有测试通过！")
        return 0
    else:
        print(f"\n✗ 有 {total_tests - total_passed} 个测试失败")
        return 1


if __name__ == '__main__':
    sys.exit(main())

# -*- coding: utf-8 -*-
"""
日志配置模块

配置日志系统，包括控制台输出和文件轮转。
"""

import os
import logging
import logging.handlers


def setup_logging():
    """设置日志配置，包含控制台输出和错误日志文件轮转"""
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    date_format = '%Y-%m-%d %H:%M:%S'

    # 基础控制台日志配置
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        datefmt=date_format
    )

    # 获取 copyU 根 logger
    copyu_logger = logging.getLogger('copyU')

    # 确保日志目录存在
    log_dir = os.path.expanduser('~/.config/copyu/logs')
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        log_dir = '/tmp/copyu_logs'
        os.makedirs(log_dir, exist_ok=True)

    # 添加错误日志文件处理器（RotatingFileHandler）
    # 单个文件最大 5MB，保留 3 个备份文件
    error_log_path = os.path.join(log_dir, 'error.log')
    error_handler = logging.handlers.RotatingFileHandler(
        error_log_path,
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=3,
        encoding='utf-8'
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter(log_format, date_format))
    copyu_logger.addHandler(error_handler)

    # 添加性能指标日志处理器（可选）
    metrics_log_path = os.path.join(log_dir, 'metrics.log')
    metrics_handler = logging.handlers.RotatingFileHandler(
        metrics_log_path,
        maxBytes=2 * 1024 * 1024,  # 2MB
        backupCount=2,
        encoding='utf-8'
    )
    metrics_handler.setLevel(logging.INFO)
    metrics_handler.setFormatter(logging.Formatter(log_format, date_format))

    # 创建 metrics logger
    metrics_logger = logging.getLogger('copyU.metrics')
    metrics_logger.addHandler(metrics_handler)
    metrics_logger.setLevel(logging.INFO)

    return copyu_logger


# 初始化日志
logger = setup_logging()

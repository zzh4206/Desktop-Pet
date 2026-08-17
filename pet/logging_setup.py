"""日志地基 —— 路径与轮转见 平台适配与分工.md §五。

v0.1：日志路径（经 ``platform.py`` 传入，平台无关）+ 按大小轮转
（``RotatingFileHandler``，2MB × 3）。``--verbose`` 额外输出到 stderr。

v0.4.13：``log_level`` 接入 config（``INFO``/``DEBUG``/``WARNING``），
文件 handler 恒 DEBUG（全量落盘便于排查），logger 级别由 config 控制（非
verbose 时不再卡到 INFO 致 ``behavior._log.debug`` 不落盘）；``--verbose``
额外加 stderr DEBUG handler。``handlers`` 只清自己挂的，不吞外部 handler。
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}


def setup_logging(
    verbose: bool, log_dir: str, log_level: str = "INFO"
) -> logging.Logger:
    logger = logging.getLogger("pet")
    # 只清自己上一轮挂的 handler（按对象标识比对，保留外部挂的）
    logger.handlers = [
        h for h in logger.handlers
        if not getattr(h, "_pet_owned", False)
    ]
    logger.propagate = False

    # logger 级别由 config log_level 控制（非 verbose 也能落 DEBUG 文件）
    level = _LEVELS.get((log_level or "INFO").upper(), logging.INFO)
    if verbose:
        level = logging.DEBUG
    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "pet.log")
    fh = RotatingFileHandler(
        path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)  # 文件恒 DEBUG 全量落盘
    fh._pet_owned = True  # 标记为自有 handler
    logger.addHandler(fh)

    if verbose:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        ch.setLevel(logging.DEBUG)
        ch._pet_owned = True
        logger.addHandler(ch)

    return logger

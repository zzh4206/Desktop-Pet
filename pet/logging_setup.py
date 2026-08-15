"""日志地基 —— 路径与轮转见 平台适配与分工.md §五。

v0.1：mac 日志路径（经 ``platform.py`` 传入）+ 按大小轮转
（``RotatingFileHandler``，2MB × 3）。``--verbose`` 额外输出到 stderr。
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler


def setup_logging(verbose: bool, log_dir: str) -> logging.Logger:
    logger = logging.getLogger("pet")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "pet.log")
    fh = RotatingFileHandler(
        path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    logger.addHandler(fh)

    if verbose:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(fmt)
        ch.setLevel(logging.DEBUG)
        logger.addHandler(ch)

    return logger

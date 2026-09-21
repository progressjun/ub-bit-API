"""표준 로깅 설정. 콘솔 + 일자별 파일."""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

_FMT = "%(asctime)s %(levelname)-7s %(name)-14s %(message)s"


def setup_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    os.makedirs(log_dir, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(_FMT))
    root.addHandler(console)

    fileh = RotatingFileHandler(
        os.path.join(log_dir, "ubbit.log"), maxBytes=10 * 1024 * 1024, backupCount=10,
        encoding="utf-8",
    )
    fileh.setFormatter(logging.Formatter(_FMT))
    root.addHandler(fileh)

    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

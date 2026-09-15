from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

SECRET_KEYS = ("KEY", "TOKEN", "SECRET", "PRIVATE", "PASSPHRASE", "PASSWORD")


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.getMessage())
        for k in SECRET_KEYS:
            if k.lower() in msg.lower() and "sk-" in msg.lower():
                record.msg = "[redacted]"
                record.args = ()
        return True


def setup_logging(path: Path | None = None) -> logging.Logger:
    log = logging.getLogger("polygrok")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.addFilter(RedactFilter())
    log.addHandler(sh)
    if path:
        fh = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5)
        fh.setFormatter(fmt)
        fh.addFilter(RedactFilter())
        log.addHandler(fh)
    log.propagate = False
    return log

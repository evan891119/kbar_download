"""Minimal credential file reader: no execution, interpolation or environment mutation."""
import os
from pathlib import Path

from .model import DataError

KEYS = ("SJ_API_KEY", "SJ_SEC_KEY")


def load_credentials(path):
    """Read UTF-8 KEY=VALUE entries; existing environment variables take priority."""
    values = {}
    path = Path(path)
    if path.exists():
        for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not separator or key not in KEYS or key in values:
                raise DataError(".env 第 {} 行格式不正確、欄位未知或重複".format(number))
            if value[:1] in ("'", '"'):
                if len(value) < 2 or value[-1] != value[0]:
                    raise DataError(".env 第 {} 行引號未完整配對".format(number))
                value = value[1:-1]
            values[key] = value
    return {key: os.environ.get(key, values.get(key, "")) for key in KEYS}

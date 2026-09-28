"""Configuration, exact market timestamps and small serializable records."""
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TAIPEI = timezone(timedelta(hours=8), name="Asia/Taipei")
EPOCH = datetime(1970, 1, 1)
CATEGORIES = {
    "txf": "大台期貨 R1", "mxf": "小台期貨 R1", "tmf": "微台期貨 R1",
    "tsmc_stock": "台積電股票", "tsmc_future": "台積電個股期貨 R1",
    "tsmc_option": "台積電選擇權",
}

class DataError(Exception):
    """Local integrity error, safe to display (never contains API exception text)."""

class ProviderError(Exception):
    def __init__(self, kind="provider_error", retryable=False):
        self.kind = kind
        self.retryable = retryable
        super().__init__(kind)

class QuotaPause(Exception):
    pass

@dataclass
class Config:
    output_dir: str = "data"
    chunk_days: int = 7
    reserve_bytes: int = 50 * 1024 * 1024
    reserve_ratio: float = 0.1
    initial_daily_bytes: int = 1024 * 1024
    safety_factor: float = 2.0
    request_interval_seconds: float = 1.0
    quota_poll_seconds: float = 1800
    retry_attempts: int = 3
    retry_backoff_seconds: float = 5
    timeout_ms: int = 30000
    cutoff: str = None
    start_overrides: dict = field(default_factory=dict)
    confirmed_empty: list = field(default_factory=list)

    @classmethod
    def load(cls, path):
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) - set(cls.__dataclass_fields__):
            raise DataError("設定欄位未知或不是 JSON object")
        cfg = cls(**raw)
        cfg.validate()
        cfg.output_dir = str((Path(path).resolve().parent / cfg.output_dir).resolve())
        return cfg

    def validate(self):
        for key, low, high in [("chunk_days", 1, 30), ("reserve_bytes", 0, 10**12),
                               ("initial_daily_bytes", 1, 10**12), ("retry_attempts", 1, 10),
                               ("timeout_ms", 1, 300000)]:
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise DataError("設定值不合法: " + key)
        for key, low, high in [("reserve_ratio", 0, .9), ("safety_factor", 1, 100),
                               ("request_interval_seconds", .25, 3600),
                               ("quota_poll_seconds", 60, 86400),
                               ("retry_backoff_seconds", 1, 3600)]:
            value = getattr(self, key)
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise DataError("設定值不合法: " + key)
        if not isinstance(self.output_dir, str) or not self.output_dir:
            raise DataError("output_dir 必須是路徑")
        if self.cutoff is not None:
            parse_cutoff(self.cutoff)
        if not isinstance(self.start_overrides, dict) or set(self.start_overrides) - set(CATEGORIES):
            raise DataError("start_overrides 必須使用已知商品類別")
        for value in self.start_overrides.values():
            date.fromisoformat(value)
        if not isinstance(self.confirmed_empty, list):
            raise DataError("confirmed_empty 必須是陣列")
        for item in self.confirmed_empty:
            if set(item) != {"contract_id", "start", "end", "evidence"} or not item["evidence"].strip():
                raise DataError("無資料證據需有 contract_id/start/end/evidence")
            if date.fromisoformat(item["start"]) > date.fromisoformat(item["end"]):
                raise DataError("無資料證據日期顛倒")

def parse_cutoff(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise DataError("cutoff 須包含時區，例如 +08:00")
    return dt.astimezone(TAIPEI)

def now_taipei():
    return datetime.now(TAIPEI)

def market_ns(dt):
    # SDK ts encodes Taiwan wall time, NOT Unix UTC. Never add another eight hours.
    delta = dt.replace(tzinfo=None) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000

def market_time(ts):
    seconds, ns = divmod(ts, 10**9)
    return EPOCH + timedelta(seconds=seconds, microseconds=ns // 1000)

def json_text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

def safe_component(value):
    if not value or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in value):
        raise DataError("合約識別含不允許的字元")
    return value

@dataclass
class Contract:
    category: str
    code: str
    security_type: str
    exchange: str
    start: str
    info: dict = field(default_factory=dict)
    limitations: list = field(default_factory=list)

    @property
    def identity(self):
        return "_".join(safe_component(v) for v in (self.security_type, self.exchange, self.code))

    @property
    def directory(self):
        return self.category + "/" + self.identity

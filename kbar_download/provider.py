"""Shioaji 1.7 Contract V2 adapter; no trading/account queries or CA access."""
import os
import time
from datetime import date, datetime
from enum import Enum
from importlib.metadata import version

from .model import Contract, ProviderError

SDK_VERSION = "1.7.6"
HISTORY_LIMIT = "當前商品清單不包含所有已到期契約；不得據此宣稱選擇權全歷史完整。"


def scalar(value):
    if isinstance(value, Enum) or isinstance(getattr(value, "value", None), str):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: scalar(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scalar(v) for v in value]
    return value


def info_dict(info):
    keys = ("name", "root", "delivery_month", "delivery_date", "last_trading_date",
            "begin_date", "underlying_kind", "underlying_code", "strike_price",
            "option_right", "multiplier", "contract_size", "spec_kind", "target_code")
    return {key: scalar(getattr(info, key, None)) for key in keys}


class ShioajiProvider:
    def __init__(self, api, timeout_ms=30000, interval=0):
        self.api = api
        self.timeout_ms = timeout_ms
        self.interval = interval
        self.last_call = 0.0

    @classmethod
    def connect(cls, timeout_ms=30000, interval=1.0, credentials=None):
        # Only invoked by the explicit `download` command, never by status/doctor/tests.
        if version("shioaji") != SDK_VERSION:
            raise ProviderError("sdk_version_mismatch")
        import shioaji
        credentials = os.environ if credentials is None else credentials
        key, secret = credentials.get("SJ_API_KEY"), credentials.get("SJ_SEC_KEY")
        if not key or not secret:
            raise ProviderError("missing_credentials")
        api = shioaji.Shioaji(simulation=False)
        try:
            api.login(api_key=key, secret_key=secret, subscribe_trade=False)
        except Exception:
            try:
                api.logout()
            except Exception:
                pass
            raise ProviderError("login_failed") from None
        return cls(api, timeout_ms, interval)

    def call(self, operation, *args, **kwargs):
        delay = self.interval - (time.monotonic() - self.last_call)
        if delay > 0:
            time.sleep(delay)
        self.last_call = time.monotonic()
        try:
            return operation(*args, **kwargs)
        except (TimeoutError, ConnectionError):
            raise ProviderError("transport_error", retryable=True) from None
        except Exception:
            # Upstream messages may include account/credential data. Do not persist them.
            raise ProviderError("sdk_query_failed") from None

    def close(self):
        self.call(self.api.logout)

    def usage(self):
        obj = self.call(self.api.usage)
        return {key: getattr(obj, key) for key in ("bytes", "limit_bytes", "remaining_bytes")}

    def kbars(self, contract, start, end):
        base = self.call(self.api.contracts.get, contract.code)
        if base is None:
            raise ProviderError("contract_no_longer_available")
        if (scalar(base.security_type) != contract.security_type or
                scalar(base.exchange) != contract.exchange):
            raise ProviderError("contract_identity_changed")
        result = self.call(self.api.kbars, contract=base, start=start, end=end,
                           timeout=self.timeout_ms)
        return result.dict()

    def discover(self):
        contracts, issues = [], []
        master = self.api.contracts

        def add(category, base, info, start, notes=None):
            if base is None or info is None:
                raise ProviderError("contract_not_available")
            contracts.append(Contract(category, str(base.code), str(scalar(base.security_type)),
                                      str(scalar(base.exchange)), start, info_dict(info), notes or []))

        for category, root in (("txf", "TXF"), ("mxf", "MXF"), ("tmf", "TMF")):
            try:
                # Candidate roots are documented; accept aliases only after live validation.
                base = self.call(master.get, root + "R1")
                info = self.call(master.info, base) if base is not None else None
                if info is None or scalar(base.security_type) != "FUT" or info.root != root:
                    raise ProviderError("r1_not_available")
                # Do not use R1's *current target* begin_date to truncate continuous history.
                start = "2024-07-29" if category == "tmf" else "2020-03-22"
                add(category, base, info, start, ["R1 連續歷史；非全部個別到期合約。"])
            except ProviderError as exc:
                issues.append({"category": category, "reason": exc.kind})
        stock = None
        try:
            stock = self.call(master.get, "2330")
            info = self.call(master.info, stock) if stock is not None else None
            if info is None or scalar(stock.security_type) != "STK" or "積電" not in (info.name or ""):
                raise ProviderError("tsmc_stock_not_verified")
            add("tsmc_stock", stock, info, "2020-03-02")
        except ProviderError as exc:
            stock = None
            issues.append({"category": "tsmc_stock", "reason": exc.kind})
        try:
            if stock is None:
                raise ProviderError("underlying_not_verified")
            rows = self.call(master.futures_by_underlying, stock)
            roots = sorted({row.root for row in rows if row.root and str(row.underlying_code) == "2330"})
            found = 0
            for root in roots:
                base = self.call(master.get, root + "R1")
                if base is None:
                    issues.append({"category": "tsmc_future", "reason": "r1_not_available", "root": root})
                    continue
                info = self.call(master.info, base)
                if (info is None or str(info.underlying_code) != "2330" or
                        scalar(base.security_type) != "FUT" or info.root != root):
                    issues.append({"category": "tsmc_future", "reason": "underlying_mismatch", "root": root})
                    continue
                add("tsmc_future", base, info, "2020-03-22", ["商品規格分開保存；不以當月 begin_date 截斷 R1 歷史。"])
                found += 1
            if not found:
                raise ProviderError("no_tsmc_future_r1")
        except ProviderError as exc:
            issues.append({"category": "tsmc_future", "reason": exc.kind})
        try:
            # Inspect every listed root: name-only matching could omit adjusted/mini roots.
            roots = self.call(master.option_roots)
            seen = set()
            for root, _name in roots:
                try:
                    rows = self.call(master.options, root)
                    for info in rows:
                        if str(info.underlying_code) != "2330":
                            continue
                        base = info.base
                        if scalar(base.security_type) != "OPT" or base.code in seen:
                            continue
                        seen.add(base.code)
                        begin = scalar(getattr(info, "begin_date", None))
                        if not begin:
                            issues.append({"category": "tsmc_option", "code": base.code,
                                           "reason": "begin_date_unknown_no_safe_history_lower_bound"})
                            continue
                        add("tsmc_option", base, info, begin, [HISTORY_LIMIT])
                except ProviderError as exc:
                    issues.append({"category": "tsmc_option", "root": root, "reason": exc.kind})
            if not seen:
                issues.append({"category": "tsmc_option", "reason": "no_listed_tsmc_options"})
        except ProviderError as exc:
            issues.append({"category": "tsmc_option", "reason": exc.kind})
        return contracts, issues

"""Single-run backfill. API effects are injectable for offline failure testing."""
import copy
import math
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta

from .model import (CATEGORIES, Contract, DataError, ProviderError, QuotaPause,
                    TAIPEI, json_text, market_ns, market_time, now_taipei, parse_cutoff)
from .provider import HISTORY_LIMIT, SDK_VERSION
from .storage import atomic_write, encoded_json


def validate_bars(payload, start, end, cutoff):
    if not isinstance(payload, dict) or "ts" not in payload:
        raise DataError("回應缺少 ts；不能視為無資料")
    for key, values in payload.items():
        if not isinstance(key, str) or not key or not isinstance(values, (list, tuple)):
            raise DataError("Kbars 必須是欄位名稱對應陣列")
    count = len(payload["ts"])
    if any(len(values) != count for values in payload.values()):
        raise DataError("Kbars 各欄長度不一致")
    required = {"ts", "Open", "High", "Low", "Close", "Volume", "Amount"}
    if count and not required.issubset(payload):
        raise DataError("非空 Kbars 缺少必要來源欄位")
    rows, seen, trimmed = [], {}, 0
    for index, ts in enumerate(payload["ts"]):
        if type(ts) is not int or ts % (60 * 10**9):
            raise DataError("ts 必須是整數奈秒且對齊分鐘")
        dt = market_time(ts)
        if not start <= dt.date() <= end:
            raise DataError("來源時間超出查詢日曆日期；需核對夜盤日期語義")
        row = {key: values[index] for key, values in payload.items()}
        try:
            json_text(row)  # Exact ints; no NaN, Infinity or unsupported objects.
        except (ValueError, TypeError):
            raise DataError("來源欄位無法無損序列化") from None
        for key in required - {"ts"}:
            if type(row[key]) not in (int, float) or not math.isfinite(row[key]):
                raise DataError("OHLCVA 欄位不是有限數值")
        if row["Low"] > min(row["Open"], row["Close"]) or row["High"] < max(row["Open"], row["Close"]) or row["Low"] > row["High"]:
            raise DataError("OHLC 值不一致")
        if row["Volume"] < 0 or row["Amount"] < 0:
            raise DataError("成交量或金額為負值")
        if ts > market_ns(cutoff):
            trimmed += 1
            continue
        if ts in seen and seen[ts] != row:
            raise DataError("單次回應同時間戳有不同值")
        seen[ts] = row
    rows = [seen[ts] for ts in sorted(seen)]
    return rows, trimmed


def make_jobs(contract, cutoff, chunk_days):
    start = date.fromisoformat(contract.start)
    stop = cutoff.date()
    # Options are dated contracts; R1 delivery/begin belong to the current target only.
    if contract.security_type == "OPT" and contract.info.get("last_trading_date"):
        stop = min(stop, date.fromisoformat(contract.info["last_trading_date"]))
    result = []
    while start <= stop:
        days = 1 if not result else chunk_days
        end = min(start + timedelta(days=days - 1), stop)
        result.append({"contract_id": contract.identity, "start": start.isoformat(),
                       "end": end.isoformat(), "status": "pending", "attempts": 0})
        start = end + timedelta(days=1)
    return result


def checked_usage(provider):
    usage = provider.usage()
    if any(type(usage.get(k)) is not int for k in ("bytes", "limit_bytes", "remaining_bytes")):
        raise ProviderError("invalid_usage")
    if usage["limit_bytes"] <= 0 or usage["bytes"] < 0 or not 0 <= usage["remaining_bytes"] <= usage["limit_bytes"]:
        raise ProviderError("invalid_usage")
    return usage


class Engine:
    def __init__(self, config, provider, store, *, clock=now_taipei, sleep=time.sleep,
                 wait_quota=True, retry_unresolved=False, emit=print):
        self.cfg, self.provider, self.store = config, provider, store
        self.clock, self.sleep, self.emit = clock, sleep, emit
        self.wait_quota, self.retry_unresolved = wait_quota, retry_unresolved
        self.state = None

    def initialize(self):
        state = self.store.load()
        if state is not None:
            if self.cfg.cutoff and parse_cutoff(self.cfg.cutoff) != parse_cutoff(state["cutoff"]):
                raise DataError("此輸出目錄已固定截止時間；新範圍請使用另一個目錄")
            if self.cfg.start_overrides != state["start_overrides"]:
                raise DataError("續傳不能變更起點；新範圍請使用另一個目錄")
            self.state = state
            if self.retry_unresolved:
                self.refresh_discovery()
                for job in state["jobs"]:
                    if job["status"] in ("unresolved_empty", "error"):
                        job["status"] = "pending"
            return
        if any(self.store.root.rglob("*.csv")):
            raise DataError("新回補必須使用沒有既有 CSV 的目錄")
        cutoff = parse_cutoff(self.cfg.cutoff) if self.cfg.cutoff else self.clock().replace(second=0, microsecond=0) - timedelta(minutes=1)
        if cutoff > self.clock():
            raise DataError("截止時間不能是未來")
        checked_usage(self.provider)  # Do not enumerate shards when quota cannot be queried.
        contracts, issues = self.provider.discover()
        self.state = {"format_version": 1, "sdk_version": SDK_VERSION, "cutoff": cutoff.isoformat(),
                      "created_at": self.clock().isoformat(), "status": "running", "contracts": [],
                      "discovery_issues": issues, "jobs": [], "files": {},
                      "start_overrides": self.cfg.start_overrides, "daily_estimates": {},
                      "limitations": [HISTORY_LIMIT, "非空回應僅證明取得該次 API 回傳；不證明每分鐘完整或無歷史缺漏。",
                                      "執行當日尾段可能尚未發布或仍在修訂；不自動延伸固定截止時間。"]}
        seen = set()
        for contract in contracts:
            if contract.identity in seen:
                continue
            seen.add(contract.identity)
            if contract.category in self.cfg.start_overrides:
                contract.limitations.append("使用者指定查詢起點；不能宣稱完整歷史。")
                contract.start = self.cfg.start_overrides[contract.category]
            self.state["contracts"].append(asdict(contract))
            self.state["jobs"].extend(make_jobs(contract, cutoff, self.cfg.chunk_days))
        self.store.save(self.state)

    def refresh_discovery(self):
        """Retry discovery without dropping saved contracts or extending the cutoff."""
        contracts, issues = self.provider.discover()
        self.state["discovery_issues"] = issues
        seen = {Contract(**c).identity for c in self.state["contracts"]}
        cutoff = parse_cutoff(self.state["cutoff"])
        for contract in contracts:
            if contract.identity in seen or date.fromisoformat(contract.start) > cutoff.date():
                continue
            if contract.category in self.cfg.start_overrides:
                contract.start = self.cfg.start_overrides[contract.category]
                contract.limitations.append("使用者指定查詢起點；不能宣稱完整歷史。")
            self.state["contracts"].append(asdict(contract))
            self.state["jobs"].extend(make_jobs(contract, cutoff, self.cfg.chunk_days))
            seen.add(contract.identity)
        self.save()

    def save(self):
        self.store.save(self.state)

    def reserve(self, usage):
        return max(self.cfg.reserve_bytes, math.ceil(usage["limit_bytes"] * self.cfg.reserve_ratio))

    def estimate(self, job):
        days = (date.fromisoformat(job["end"]) - date.fromisoformat(job["start"])).days + 1
        daily = self.state["daily_estimates"].get(job["contract_id"], self.cfg.initial_daily_bytes)
        return math.ceil(days * daily * self.cfg.safety_factor)

    def quota_ready(self, job):
        usage = checked_usage(self.provider)
        self.state["last_usage"] = usage
        self.state["last_usage_at"] = self.clock().isoformat()
        if usage["remaining_bytes"] >= self.reserve(usage) + self.estimate(job):
            self.state.pop("quota_wait", None)
            job["status"] = "pending"
            self.state["status"] = "running"
            return usage
        # Shrink the next request to one day before waiting for a reset.
        if job["start"] != job["end"]:
            remainder = copy.deepcopy(job)
            remainder["start"] = (date.fromisoformat(job["start"]) + timedelta(days=1)).isoformat()
            remainder["status"] = "pending"
            job["end"] = job["start"]
            self.state["jobs"].insert(self.state["jobs"].index(job) + 1, remainder)
            if usage["remaining_bytes"] >= self.reserve(usage) + self.estimate(job):
                return usage
        if usage["limit_bytes"] < self.reserve(usage) + self.estimate(job):
            raise ProviderError("configured_buffer_or_estimate_exceeds_daily_limit")
        job["status"] = "waiting_quota"
        previous = self.state.get("quota_wait")
        if not previous:
            now = self.clock()
            check_at = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if check_at <= now:
                check_at += timedelta(days=1)
            # Weekday is only a candidate, never proof of an exchange trading day/reset.
            while check_at.weekday() >= 5:
                check_at += timedelta(days=1)
            self.state["quota_wait"] = {"check_at": check_at.isoformat(), "usage": usage}
        self.state["status"] = "waiting_quota"
        self.save()
        self.report()
        if not self.wait_quota:
            raise QuotaPause()
        self.emit("額度不足，進度已保存；等待 08:00 後 API 確認額度恢復。")
        while True:
            wait = self.state["quota_wait"]
            delay = max(0, (parse_cutoff(wait["check_at"]) - self.clock()).total_seconds())
            # Interruptible sleeps; the persisted check time also survives process restarts.
            if delay:
                self.sleep(min(delay, 60))
                continue
            current = checked_usage(self.provider)
            before = wait["usage"]
            improved = current["remaining_bytes"] > before["remaining_bytes"] or current["bytes"] < before["bytes"]
            if improved and current["remaining_bytes"] >= self.reserve(current) + self.estimate(job):
                self.state.pop("quota_wait", None)
                job["status"] = "pending"
                self.state["status"] = "running"
                self.save()
                return current
            wait["check_at"] = (self.clock() + timedelta(seconds=self.cfg.quota_poll_seconds)).isoformat()
            self.save()

    def empty_evidence(self, job):
        for record in self.cfg.confirmed_empty:
            if (record["contract_id"] == job["contract_id"] and
                    record["start"] <= job["start"] and record["end"] >= job["end"]):
                return record["evidence"]
        return None

    def process(self, job, contract):
        before = self.quota_ready(job)
        self.sleep(self.cfg.request_interval_seconds)
        payload = None
        for attempt in range(self.cfg.retry_attempts):
            job["attempts"] += 1
            try:
                payload = self.provider.kbars(contract, job["start"], job["end"])
                break
            except ProviderError as exc:
                if not exc.retryable or attempt + 1 == self.cfg.retry_attempts:
                    raise
                self.save()
                self.sleep(self.cfg.retry_backoff_seconds * 2**attempt)
                before = self.quota_ready(job)
        after = checked_usage(self.provider)
        self.state["last_usage"] = after
        self.state["last_usage_at"] = self.clock().isoformat()
        days = (date.fromisoformat(job["end"]) - date.fromisoformat(job["start"])).days + 1
        observed = max(0, after["bytes"] - before["bytes"])
        prior = self.state["daily_estimates"].get(contract.identity, self.cfg.initial_daily_bytes)
        job["usage_before"], job["usage_after"] = before, after
        job["queried_at"] = self.clock().isoformat()
        rows, trimmed = validate_bars(payload, date.fromisoformat(job["start"]),
                                     date.fromisoformat(job["end"]), parse_cutoff(self.state["cutoff"]))
        job["trimmed_after_cutoff"] = trimmed
        if not rows:
            # Empty response can mean traffic exhaustion, never infer holiday from weekdays.
            if after["remaining_bytes"] <= self.reserve(after):
                job["status"] = "waiting_quota"
                self.save()
                return False
            evidence = self.empty_evidence(job)
            job["status"] = "confirmed_empty" if evidence else "unresolved_empty"
            job["reason"] = evidence or "空回應：尚無休市／無成交／上市前／權限或發布延遲的可靠證據"
            self.save()
            return True
        self.state["daily_estimates"][contract.identity] = max(prior, math.ceil(observed / days))
        updated = copy.deepcopy(self.state)
        current = updated["jobs"][self.state["jobs"].index(job)]
        files = self.store.merge(contract, rows, updated)
        current.update(status="done", rows=len(rows), first_ts=rows[0]["ts"], last_ts=rows[-1]["ts"],
                       files=sorted(files))
        self.store.commit(updated, files)
        self.state = updated
        return True

    def run(self):
        self.initialize()
        contracts = {Contract(**c).identity: Contract(**c) for c in self.state["contracts"]}
        try:
            index = 0
            while index < len(self.state["jobs"]):
                job = self.state["jobs"][index]
                if job["status"] not in ("pending", "waiting_quota"):
                    index += 1
                    continue
                try:
                    finished = self.process(job, contracts[job["contract_id"]])
                    if not finished:
                        continue
                except DataError as exc:
                    job["status"], job["reason"] = "error", str(exc)
                    self.save()
                except ProviderError as exc:
                    job["status"], job["reason"] = "error", exc.kind
                    self.save()
                    # Access/transport/usage failures may affect every contract. Stop the run.
                    if exc.kind not in ("contract_no_longer_available", "contract_identity_changed"):
                        self.state["status"] = "stopped_error"
                        self.save()
                        self.report()
                        return 2
                index += 1
            self.state["status"] = "finished_with_limits"
            self.save()
        except QuotaPause:
            self.report()
            return 3
        self.report()
        has_gaps = any(j["status"] not in ("done", "confirmed_empty") for j in self.state["jobs"])
        return 2 if has_gaps or self.state["discovery_issues"] or not self.state["files"] else 0

    def report(self):
        summary = {"status": self.state["status"], "cutoff": self.state["cutoff"],
                   "limitations": self.state["limitations"], "categories": {},
                   "discovery_issues": self.state["discovery_issues"]}
        lines = ["# 回補執行報告", "", "狀態：" + summary["status"], "固定截止：" + summary["cutoff"], "",
                 "API 回傳資料已保存不等於市場全歷史完整；以下限制與缺口均須保留。", ""]
        for category, label in CATEGORIES.items():
            selected = [Contract(**c) for c in self.state["contracts"] if c["category"] == category]
            items = []
            lines.extend(["## " + label, ""])
            if not selected:
                lines.append("受限：未發現或未能驗證可查詢合約。")
            for contract in selected:
                jobs = [j for j in self.state["jobs"] if j["contract_id"] == contract.identity]
                files = [v for k, v in self.state["files"].items() if k.startswith(contract.directory + "/")]
                gaps = [j for j in jobs if j["status"] not in ("done", "confirmed_empty")]
                item = {"contract_id": contract.identity, "info": contract.info, "start": contract.start,
                        "rows": sum(f["rows"] for f in files), "gaps": gaps,
                        "confirmed_empty": [j for j in jobs if j["status"] == "confirmed_empty"],
                        "limitations": contract.limitations,
                        "first": market_time(min(f["first_ts"] for f in files)).isoformat() if files else None,
                        "last": market_time(max(f["last_ts"] for f in files)).isoformat() if files else None}
                items.append(item)
                lines.append("- {}：{} 筆，{} 至 {}，未解決區段 {}。".format(contract.identity, item["rows"], item["first"], item["last"], len(gaps)))
                for note in contract.limitations:
                    lines.append("  - 限制：" + note)
                if not files:
                    lines.append("  受限：尚未取得任何 K 棒，不能視為歷史回補成功。")
                for job in gaps:
                    lines.append("  - {}～{}：{} {}".format(job["start"], job["end"], job["status"], job.get("reason", "")))
            summary["categories"][category] = items
            lines.append("")
        lines += ["## 限制", ""] + ["- " + s for s in summary["limitations"]]
        for issue in summary["discovery_issues"]:
            lines.append("- 商品發現受限：" + json_text(issue))
        atomic_write(self.store.root / "report.json", encoded_json(summary))
        atomic_write(self.store.root / "report.md", ("\n".join(lines) + "\n").encode("utf-8"))
        atomic_write(self.store.root / "metadata.json", encoded_json({
            "sdk_version": SDK_VERSION, "contracts": self.state["contracts"],
            "cutoff": self.state["cutoff"], "csv_cell_encoding": "JSON scalar; blank means field absent",
            "timestamp": "SDK ts: integer nanoseconds encoding Asia/Taipei wall clock; no UTC conversion",
            "files": self.state["files"]}))

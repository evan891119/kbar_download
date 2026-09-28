import copy
import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as Obj
from unittest.mock import patch

from kbar_download.cli import main
from kbar_download.engine import Engine, validate_bars, make_jobs
from kbar_download.model import Config, Contract, DataError, ProviderError, TAIPEI, market_ns
from kbar_download.provider import ShioajiProvider, scalar
from kbar_download.storage import Store


def stamp(day, hour=9, minute=1):
    return market_ns(datetime.fromisoformat(day).replace(hour=hour, minute=minute))


def bars(*timestamps, **extra):
    result = {"ts": list(timestamps), "Open": [10.25] * len(timestamps),
              "High": [11.5] * len(timestamps), "Low": [9] * len(timestamps),
              "Close": [11] * len(timestamps), "Volume": [100] * len(timestamps),
              "Amount": [1100] * len(timestamps)}
    result.update(extra)
    return result


class Fake:
    def __init__(self, contracts, payload=None):
        self.contracts = contracts
        self.payload = payload or (lambda c, a, b: bars(stamp(a)))
        self.calls = []
        self.used = 0
        self.limit = 10**9
        self.issues = []

    def discover(self):
        return copy.deepcopy(self.contracts), self.issues

    def usage(self):
        return {"bytes": self.used, "limit_bytes": self.limit, "remaining_bytes": self.limit - self.used}

    def kbars(self, contract, start, end):
        self.calls.append((contract.code, start, end))
        self.used += 100
        return self.payload(contract, start, end)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.cfg = Config(cutoff="2026-02-02T23:59:00+08:00", reserve_bytes=100,
                          reserve_ratio=0, initial_daily_bytes=100)
        self.contract = Contract("tsmc_stock", "2330", "STK", "TSE", "2026-01-31")
        self.clock_value = datetime(2026, 2, 3, 10, tzinfo=TAIPEI)

    def engine(self, fake, **kwargs):
        return Engine(self.cfg, fake, self.store, clock=lambda: self.clock_value,
                      sleep=lambda s: None, emit=lambda s: None, **kwargs)

    def test_cross_month_resume_no_duplicate_no_new_dates(self):
        fake = Fake([self.contract], lambda c, a, b: bars(stamp(a), stamp(b)))
        self.assertEqual(self.engine(fake).run(), 0)
        calls = list(fake.calls)
        state = self.store.load()
        self.assertEqual(len(state["files"]), 2)
        self.assertEqual(sum(v["rows"] for v in state["files"].values()), 3)
        self.clock_value += timedelta(days=20)
        self.assertEqual(self.engine(fake).run(), 0)
        self.assertEqual(fake.calls, calls)
        self.assertEqual(self.store.load()["cutoff"], self.cfg.cutoff)

    def test_exact_all_fields_and_schema_expansion(self):
        self.contract.start = "2026-02-01"
        fake = Fake([self.contract], lambda c, a, b: bars(stamp(a),
                    unusual=[{"note": "逗號,換行\n", "big": 9007199254740993}],
                    nullable=[None], empty_string=[""], **({"new_field": [True]} if a.endswith("02") else {})))
        self.engine(fake).run()
        path = next(Path(self.tmp.name).rglob("*.csv"))
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(json.loads(rows[0]["unusual"])["big"], 9007199254740993)
        self.assertEqual(json.loads(rows[0]["nullable"]), None)
        self.assertEqual(json.loads(rows[0]["empty_string"]), "")
        self.assertEqual(rows[0]["new_field"], "")
        self.assertEqual(json.loads(rows[1]["new_field"]), True)
        self.assertEqual(int(rows[0]["ts"]), stamp("2026-02-01"))

    def test_empty_is_unresolved_and_can_retry(self):
        fake = Fake([self.contract], lambda *args: bars())
        self.assertEqual(self.engine(fake).run(), 2)
        self.assertTrue(all(j["status"] == "unresolved_empty" for j in self.store.load()["jobs"]))
        fake.payload = lambda c, a, b: bars(stamp(a))
        self.assertEqual(self.engine(fake, retry_unresolved=True).run(), 0)

    def test_empty_requires_explicit_evidence(self):
        self.cfg.confirmed_empty = [{"contract_id": self.contract.identity, "start": "2026-01-31",
                                    "end": "2026-02-02", "evidence": "fixture calendar"}]
        self.engine(Fake([self.contract], lambda *args: bars())).run()
        self.assertTrue(all(j["status"] == "confirmed_empty" for j in self.store.load()["jobs"]))

    def test_bad_response_does_not_advance(self):
        self.engine(Fake([self.contract], lambda *args: bars(stamp("2026-01-31"), extra=[]))).run()
        self.assertTrue(all(j["status"] == "error" for j in self.store.load()["jobs"]))
        self.assertFalse(list(Path(self.tmp.name).rglob("*.csv")))

    def test_quota_pause_restart(self):
        fake = Fake([self.contract])
        fake.limit, fake.used = 1000, 950
        self.assertEqual(self.engine(fake, wait_quota=False).run(), 3)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.store.load()["status"], "waiting_quota")
        fake.used = 0
        self.assertEqual(self.engine(fake, wait_quota=False).run(), 0)

    def test_quota_does_not_resume_at_midnight_or_without_api_reset(self):
        fake = Fake([self.contract])
        fake.limit, fake.used = 1000, 950
        checks = []
        original = fake.usage
        def usage():
            checks.append(self.clock_value)
            if self.clock_value >= datetime(2026, 2, 4, 8, 30, tzinfo=TAIPEI):
                fake.used = 0
            return original()
        fake.usage = usage
        def advance(seconds):
            self.clock_value += timedelta(seconds=seconds)
        engine = self.engine(fake)
        engine.sleep = advance
        self.assertEqual(engine.run(), 0)
        self.assertTrue(any(x.hour == 8 and x.minute == 0 for x in checks))
        self.assertFalse(any(x.date().isoformat() == "2026-02-04" and x.hour < 8 for x in checks))
        self.assertGreaterEqual(self.clock_value, datetime(2026, 2, 4, 8, 30, tzinfo=TAIPEI))

    def test_quota_shrinks_multiday_window(self):
        self.contract.start = "2026-01-01"
        fake = Fake([self.contract])
        fake.limit, fake.used = 1000, 0
        self.engine(fake, wait_quota=False).run()
        self.assertTrue(all(a == b for _, a, b in fake.calls))
        self.assertEqual(self.store.load()["status"], "waiting_quota")

    def test_corrupt_csv_stops_before_queries(self):
        fake = Fake([self.contract])
        self.engine(fake).run()
        next(Path(self.tmp.name).rglob("*.csv")).write_text("broken")
        calls = len(fake.calls)
        with self.assertRaises(DataError):
            self.engine(fake).run()
        self.assertEqual(len(fake.calls), calls)

    def test_atomic_recovery_after_first_month_replaced(self):
        fake = Fake([self.contract], lambda c, a, b: bars(stamp(a), stamp(b)))
        engine = self.engine(fake)
        engine.initialize()
        # Force one cross-month transaction to exercise partial rename recovery.
        engine.state["jobs"] = [{"contract_id": self.contract.identity, "start": "2026-01-31",
                                 "end": "2026-02-02", "status": "pending", "attempts": 0}]
        engine.save()
        import kbar_download.storage as storage
        real_replace = storage.os.replace
        replacements = []
        def crash(src, dst):
            if str(dst).endswith(".csv"):
                replacements.append(dst)
                if len(replacements) == 2:
                    raise OSError("simulated disk interruption")
            return real_replace(src, dst)
        with patch.object(storage.os, "replace", side_effect=crash):
            with self.assertRaises(OSError):
                engine.run()
        self.assertTrue(self.store.journal.exists())
        calls = len(fake.calls)
        self.assertEqual(self.engine(fake).run(), 0)
        self.assertEqual(len(fake.calls), calls)
        self.assertFalse(self.store.journal.exists())
        self.assertEqual(sum(f["rows"] for f in self.store.load()["files"].values()), 2)

    def test_progress_write_failure_recovers_without_refetch(self):
        fake = Fake([self.contract])
        engine = self.engine(fake)
        engine.initialize()
        import kbar_download.storage as storage
        real = storage.atomic_write
        def fail(path, data):
            if Path(path).name == "progress.json":
                raise OSError("disk full")
            return real(path, data)
        with patch.object(storage, "atomic_write", side_effect=fail):
            with self.assertRaises(OSError):
                engine.run()
        self.assertTrue(self.store.journal.exists())
        self.engine(fake).run()
        self.assertEqual(sum(a == "2026-01-31" for _, a, b in fake.calls), 1)

    def test_lock_prevents_second_writer(self):
        with self.store.lock():
            with self.assertRaises(DataError):
                with Store(self.tmp.name).lock():
                    pass

    def test_changed_cutoff_rejected(self):
        self.engine(Fake([self.contract])).run()
        self.cfg.cutoff = "2026-02-03T23:59:00+08:00"
        with self.assertRaises(DataError):
            self.engine(Fake([self.contract])).run()

    def test_cutoff_and_nan_conflicting_duplicate(self):
        cutoff = datetime(2026, 2, 2, 9, 1, tzinfo=TAIPEI)
        rows, trimmed = validate_bars(bars(stamp("2026-02-02"), stamp("2026-02-02", 9, 2)), cutoff.date(), cutoff.date(), cutoff)
        self.assertEqual((len(rows), trimmed), (1, 1))
        for bad in [bars(stamp("2026-02-02"), Open=[float("nan")]),
                    bars(stamp("2026-02-02"), stamp("2026-02-02"), Close=[10, 11])]:
            with self.assertRaises(DataError):
                validate_bars(bad, cutoff.date(), cutoff.date(), cutoff)

    def test_bounded_retry_and_safe_provider_error(self):
        def failing(*args):
            raise ProviderError("transport_error", retryable=True)
        fake = Fake([self.contract], failing)
        self.assertEqual(self.engine(fake).run(), 2)
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(self.store.load()["status"], "stopped_error")

    def test_chunk_never_over_thirty_days(self):
        jobs = make_jobs(self.contract, datetime(2027, 1, 1), 30)
        self.assertTrue(all((datetime.fromisoformat(j["end"]) - datetime.fromisoformat(j["start"])).days < 30 for j in jobs))
        for left, right in zip(jobs, jobs[1:]):
            self.assertEqual(datetime.fromisoformat(left["end"]) + timedelta(days=1), datetime.fromisoformat(right["start"]))

    def test_quota_exhausted_empty_does_not_confirm_empty(self):
        fake = Fake([self.contract])
        fake.limit = 1000
        def exhausted(*args):
            fake.used = 1000
            return bars()
        fake.payload = exhausted
        self.assertEqual(self.engine(fake, wait_quota=False).run(), 3)
        self.assertEqual(self.store.load()["jobs"][0]["status"], "waiting_quota")
        self.assertFalse(self.store.load()["files"])

    def test_usage_failure_stops_without_advancing(self):
        fake = Fake([self.contract])
        original = fake.usage
        count = [0]
        def usage():
            count[0] += 1
            if count[0] >= 3:
                raise ProviderError("invalid_usage")
            return original()
        fake.usage = usage
        self.assertEqual(self.engine(fake).run(), 2)
        self.assertFalse(self.store.load()["files"])
        self.assertEqual(len(fake.calls), 1)

    def test_crash_before_journal_requeries_without_orphan_csv(self):
        fake = Fake([self.contract])
        engine = self.engine(fake)
        engine.initialize()
        import kbar_download.storage as storage
        real = storage.atomic_write
        def fail(path, data):
            if Path(path).name == ".journal.json":
                raise OSError("disk full")
            return real(path, data)
        with patch.object(storage, "atomic_write", side_effect=fail):
            with self.assertRaises(OSError):
                engine.run()
        self.assertFalse(list(Path(self.tmp.name).rglob("*.csv")))
        self.assertEqual(self.engine(fake).run(), 0)
        self.assertEqual(sum(f["rows"] for f in self.store.load()["files"].values()), 2)

    def test_later_discovery_preserves_existing_and_cutoff(self):
        fake = Fake([self.contract])
        self.engine(fake).run()
        first_calls = list(fake.calls)
        fake.contracts.append(Contract("tsmc_option", "CDO001C", "OPT", "TAIFEX", "2026-02-01"))
        fake.contracts.append(Contract("tsmc_option", "CDO002C", "OPT", "TAIFEX", "2026-03-01"))
        self.assertEqual(self.engine(fake, retry_unresolved=True).run(), 0)
        self.assertEqual(sum(c == "2330" for c, _, _ in fake.calls), len(first_calls))
        self.assertNotIn("CDO002C", [c["code"] for c in self.store.load()["contracts"]])

    def test_existing_value_conflict_preserves_csv(self):
        fake = Fake([self.contract])
        self.engine(fake).run()
        state = self.store.load()
        path = next(Path(self.tmp.name).rglob("*.csv"))
        original = path.read_bytes()
        with self.assertRaises(DataError):
            self.store.merge(self.contract, [{k: v[0] for k, v in bars(stamp("2026-01-31"), Close=[10]).items()}], state)
        self.assertEqual(path.read_bytes(), original)

    def test_status_and_doctor_never_connect(self):
        config = Path(self.tmp.name) / "config.json"
        config.write_text(json.dumps({"output_dir": "out"}))
        with patch.object(ShioajiProvider, "connect", side_effect=AssertionError("must not connect")):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["doctor"]), 0)
                self.assertEqual(main(["status", "--config", str(config)]), 0)


class DiscoveryTests(unittest.TestCase):
    def test_all_tsmc_option_roots_rights_and_expiries_only_r1(self):
        def info(code, kind, root, underlying="2330", **kwargs):
            base = Obj(code=code, security_type=kind, exchange="TSE" if kind == "STK" else "TAIFEX")
            return Obj(base=base, root=root, name="台積電", underlying_code=underlying,
                       begin_date="2026-01-01", **kwargs)
        stock = info("2330", "STK", "")
        futures = [info(r + "R1", "FUT", r, "IX0001" if r != "CDF" else "2330") for r in ("TXF", "MXF", "TMF", "CDF")]
        options = [info("CDO001C", "OPT", "CDO", option_right="C", delivery_month="202601"),
                   info("CDA002P", "OPT", "CDA", option_right="P", delivery_month="202602"),
                   info("TXO003C", "OPT", "TXO", "IX0001", option_right="C")]
        lookup = {i.base.code: i for i in [stock] + futures}
        master = Obj(get=lambda code: lookup[code].base if code in lookup else None,
                     info=lambda base: lookup[base.code], futures_by_underlying=lambda base: [futures[-1]],
                     option_roots=lambda: [("CDO", "台積電"), ("CDA", "調整契約"), ("TXO", "台指")],
                     options=lambda root: [i for i in options if i.root == root])
        contracts, issues = ShioajiProvider(Obj(contracts=master)).discover()
        self.assertEqual(issues, [])
        self.assertEqual({c.code for c in contracts if c.security_type == "OPT"}, {"CDO001C", "CDA002P"})
        self.assertTrue(all(c.code.endswith("R1") for c in contracts if c.security_type == "FUT"))
        self.assertEqual(next(c for c in contracts if c.code == "CDFR1").start, "2020-03-22")
        self.assertEqual(next(c for c in contracts if c.code == "TMFR1").start, "2024-07-29")
        self.assertTrue(all(c.limitations for c in contracts if c.security_type == "OPT"))

    def test_rust_enum_value_is_serializable(self):
        # PyO3 enums expose .value but do not inherit Python enum.Enum.
        rust_enum = Obj(value="C")
        self.assertEqual(scalar(rust_enum), "C")
        self.assertEqual(json.dumps(scalar({"right": rust_enum})), '{"right": "C"}')

    def test_sdk_exception_never_exposes_secret(self):
        def fail():
            raise ValueError("secret=DO_NOT_PRINT")
        with self.assertRaises(ProviderError) as caught:
            ShioajiProvider(None).call(fail)
        self.assertNotIn("DO_NOT_PRINT", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

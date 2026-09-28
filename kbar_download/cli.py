"""Explicit live command; all other commands are credential-free and offline."""
import argparse
import json
import os
import platform
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .engine import Engine
from .model import Config, DataError, ProviderError, parse_cutoff
from .provider import SDK_VERSION, ShioajiProvider
from .storage import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description="Shioaji 一次性歷史一分鐘 K 棒回補")
    parser.add_argument("command", choices=["doctor", "download", "status"],
                        help="doctor/status 離線；download 才登入及使用行情額度")
    parser.add_argument("--config", default="config.example.json")
    parser.add_argument("--no-wait", action="store_true", help="額度不足保存進度並結束，退出碼 3")
    parser.add_argument("--retry-unresolved", action="store_true", help="只重試未解決／失敗區段，可能消耗流量")
    args = parser.parse_args(argv)
    if args.command == "doctor":
        try:
            sdk = version("shioaji")
        except PackageNotFoundError:
            sdk = "未安裝"
        print(json.dumps({"python": platform.python_version(), "system": platform.system(),
                          "architecture": platform.machine(), "libc": platform.libc_ver(),
                          "shioaji_installed": sdk, "shioaji_expected": SDK_VERSION,
                          "note": "僅本機資訊；未登入、未驗證目標 Ubuntu 或 SDK 二進位可載入。"},
                         ensure_ascii=False, indent=2))
        return 0
    provider = None
    engine = None
    try:
        cfg = Config.load(args.config)
        store = Store(cfg.output_dir)
        with store.lock():
            if args.command == "status":
                state = store.load()
                if state is None:
                    print("尚無進度。")
                    return 0
                engine = Engine(cfg, None, store)
                engine.state = state
                engine.report()
                print("{}；截止 {}；報告 {}".format(state["status"], state["cutoff"], store.root / "report.md"))
                return 0
            # Validate/recover data before reading credentials or opening a connection.
            state = store.load()
            if state is not None:
                if cfg.cutoff and parse_cutoff(cfg.cutoff) != parse_cutoff(state["cutoff"]):
                    raise DataError("此目錄已固定截止時間；新範圍請使用另一個目錄")
                if cfg.start_overrides != state["start_overrides"]:
                    raise DataError("續傳不能變更起點；新範圍請使用另一個目錄")
            if state is not None and state["status"] == "finished_with_limits" and not args.retry_unresolved:
                engine = Engine(cfg, None, store)
                engine.state = state
                engine.report()
                print("本次回補已結束，未登入；未解決區段可用 --retry-unresolved 重試。")
                return 2 if any(j["status"] not in ("done", "confirmed_empty") for j in state["jobs"]) or state["discovery_issues"] or not state["files"] else 0
            print("輸出：{}。download 將登入並消耗行情額度。".format(store.root))
            provider = ShioajiProvider.connect(cfg.timeout_ms, cfg.request_interval_seconds)
            engine = Engine(cfg, provider, store, wait_quota=not args.no_wait,
                            retry_unresolved=args.retry_unresolved)
            code = engine.run()
            print("報告：{}；退出碼 {}".format(store.root / "report.md", code))
            return code
    except KeyboardInterrupt:
        # Never overwrite progress here: an interrupted transaction may need roll-forward.
        print("已中止；重新執行會先恢復未完成落盤交易。", file=sys.stderr)
        return 130
    except (DataError, ProviderError) as exc:
        print("停止：{}".format(exc), file=sys.stderr)
        return 2
    except (ValueError, TypeError, OSError, KeyError, PackageNotFoundError):
        # No arbitrary exception text/traceback: avoid leaking data from SDK or local config.
        print("停止：設定、檔案或依賴錯誤；請檢查 JSON、磁碟權限與套件安裝。", file=sys.stderr)
        return 2
    except Exception as exc:
        print("停止：非預期錯誤 ({})；未輸出 SDK 原始訊息。".format(type(exc).__name__), file=sys.stderr)
        return 2
    finally:
        if provider is not None:
            try:
                provider.close()
            except Exception:
                print("登出未確認。", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())

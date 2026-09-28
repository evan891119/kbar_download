"""Monthly CSV with a recoverable write-ahead transaction and advisory lock."""
import csv
import hashlib
import io
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .model import DataError, json_text, market_time


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sync_dir(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, str(path))
        sync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def encoded_json(obj):
    return (json.dumps(obj, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "progress.json"
        self.journal = self.root / ".journal.json"

    def path(self, relative):
        path = (self.root / relative).resolve()
        if self.root not in path.parents:
            raise DataError("資料路徑超出輸出目錄")
        return path

    @contextmanager
    def lock(self):
        import fcntl
        with (self.root / ".lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DataError("另一個下載器正在使用此輸出目錄") from None
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def recover(self):
        if not self.journal.exists():
            return
        transaction = json.loads(self.journal.read_text(encoding="utf-8"))
        for entry in transaction["files"]:
            target, staged = self.path(entry["path"]), self.path(entry["staged"])
            if target.exists() and digest(target.read_bytes()) == entry["sha256"]:
                continue
            if not staged.exists() or digest(staged.read_bytes()) != entry["sha256"]:
                raise DataError("交易恢復失敗：暫存檔與目標檔皆不符合校驗值")
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(staged), str(target))
            sync_dir(target.parent)
        atomic_write(self.state_path, encoded_json(transaction["state"]))
        self.journal.unlink()
        sync_dir(self.root)
        for entry in transaction["files"]:
            staged = self.path(entry["staged"])
            if staged.exists():
                staged.unlink()

    def load(self):
        self.recover()
        if not self.state_path.exists():
            return None
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if state.get("format_version") != 1:
            raise DataError("不支援的進度格式")
        for relative, record in state.get("files", {}).items():
            path = self.path(relative)
            if not path.is_file() or digest(path.read_bytes()) != record["sha256"]:
                raise DataError("CSV 缺失或被修改；請先還原備份，勿跳過校驗: " + relative)
        return state

    def save(self, state):
        atomic_write(self.state_path, encoded_json(state))

    def commit(self, state, files):
        entries = []
        for relative, data in files.items():
            target = self.path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".stage-", suffix=".tmp", dir=str(target.parent))
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            sync_dir(target.parent)
            entries.append({"path": relative, "staged": str(Path(name).relative_to(self.root)),
                            "sha256": digest(data)})
        # A crash before this journal leaves only disposable temp files, not committed data.
        atomic_write(self.journal, encoded_json({"files": entries, "state": state}))
        self.recover()

    def merge(self, contract, rows, state):
        groups = {}
        for row in rows:
            month = market_time(row["ts"]).strftime("%Y-%m")
            groups.setdefault(month, []).append(row)
        outputs = {}
        for month, incoming in groups.items():
            relative = contract.directory + "/" + month + ".csv"
            path = self.path(relative)
            existing, fields = {}, []
            if path.exists():
                if relative not in state["files"]:
                    raise DataError("發現未登記 CSV；請使用空目錄或還原對應進度")
                with path.open(encoding="utf-8", newline="") as stream:
                    reader = csv.DictReader(stream)
                    fields = reader.fieldnames or []
                    for row in reader:
                        ts = int(row["ts"])
                        if ts in existing:
                            raise DataError("既有 CSV 含重複時間戳")
                        existing[ts] = row
            for row in incoming:
                encoded = {key: json_text(value) for key, value in row.items()}
                fields.extend(key for key in encoded if key not in fields)
                old = existing.get(row["ts"])
                if old is not None:
                    if any(old.get(k, "") not in ("", v) for k, v in encoded.items()):
                        raise DataError("相同時間戳的來源值不同；保留既有值並標記衝突")
                    old.update(encoded)
                else:
                    existing[row["ts"]] = encoded
            buffer = io.StringIO(newline="")
            writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            for ts in sorted(existing):
                writer.writerow(existing[ts])
            data = buffer.getvalue().encode("utf-8")
            outputs[relative] = data
            state["files"][relative] = {"sha256": digest(data), "rows": len(existing),
                                      "fields": fields, "first_ts": min(existing), "last_ts": max(existing)}
        return outputs

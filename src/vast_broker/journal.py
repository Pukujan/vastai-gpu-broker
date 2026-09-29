"""Durable local lease journal with request-scoped process locks."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator


class JournalError(RuntimeError):
    """A lease obligation could not be durably recorded or read."""


def _acquire_windows_lock(handle: Any, msvcrt_module: Any | None = None, *,
                          sleep_fn: Any = time.sleep) -> None:
    """Wait for the journal byte lock, retrying only Windows lock contention."""
    if msvcrt_module is None:
        import msvcrt as msvcrt_module

    while True:
        handle.seek(0, os.SEEK_SET)
        try:
            # LK_NBLCK fails immediately with EACCES when another process holds
            # the byte; unlike LK_LOCK, it has no built-in ten-attempt limit.
            msvcrt_module.locking(handle.fileno(), msvcrt_module.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno != errno.EACCES:
                raise
            sleep_fn(0.05)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"unsupported journal value type: {type(value).__name__}")


class LeaseJournal:
    """Atomic JSON records; callers hold ``locked`` across lifecycle operations."""

    SCHEMA_VERSION = 1

    def __init__(self, directory: str | os.PathLike[str]):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._local_locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()
        self._depth = threading.local()

    @staticmethod
    def _key(request_id: str) -> str:
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id must be a non-empty string")
        return hashlib.sha256(request_id.encode("utf-8")).hexdigest()

    def _record_path(self, request_id: str) -> Path:
        return self.directory / (self._key(request_id) + ".json")

    @contextmanager
    def locked(self, request_id: str) -> Iterator[None]:
        key = self._key(request_id)
        with self._guard:
            local = self._local_locks.setdefault(key, threading.RLock())
        with local:
            depths = getattr(self._depth, "values", {})
            depth = depths.get(key, 0)
            if depth:
                depths[key] = depth + 1
                self._depth.values = depths
                try:
                    yield
                finally:
                    depths[key] -= 1
                return
            lock_path = self.directory / (key + ".lock")
            with lock_path.open("a+b") as handle:
                if os.name == "nt":
                    import msvcrt
                    # Check file size without reading the lock byte: a different
                    # process may already hold the byte-range lock, and Windows
                    # can reject that read with PermissionError before we wait.
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.seek(0, os.SEEK_SET)
                        handle.write(b"0")
                        handle.flush()
                    _acquire_windows_lock(handle, msvcrt)
                    try:
                        depths[key] = 1
                        self._depth.values = depths
                        yield
                    finally:
                        depths.pop(key, None)
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        depths[key] = 1
                        self._depth.values = depths
                        yield
                    finally:
                        depths.pop(key, None)
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def load(self, request_id: str) -> dict[str, Any] | None:
        key = self._key(request_id)
        depths = getattr(self._depth, "values", {})
        if not depths.get(key, 0):
            with self.locked(request_id):
                return self.load(request_id)
        path = self._record_path(request_id)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise JournalError("lease journal record is unreadable") from exc
        if not isinstance(record, dict) or record.get("schema_version") != self.SCHEMA_VERSION:
            raise JournalError("lease journal record has an unsupported schema")
        if record.get("request_id") != request_id:
            raise JournalError("lease journal request identity mismatch")
        return record

    def save(self, record: dict[str, Any]) -> None:
        request_id = record.get("request_id")
        key = self._key(request_id)
        depths = getattr(self._depth, "values", {})
        if not depths.get(key, 0):
            with self.locked(request_id):
                self.save(record)
            return
        path = self._record_path(request_id)
        payload = dict(record)
        payload["schema_version"] = self.SCHEMA_VERSION
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False, default=_json_default)
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.directory, delete=False) as temp:
                temp_name = temp.name
                temp.write(encoded)
                temp.flush()
                os.fsync(temp.fileno())
            os.replace(temp_name, path)
            if os.name != "nt":
                fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except (OSError, TypeError, ValueError) as exc:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
            raise JournalError("lease journal record could not be persisted") from exc

    def list_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in self.directory.glob("*.json"):
            try:
                records.append(self.load(json.loads(path.read_text(encoding="utf-8")).get("request_id")))
            except (OSError, json.JSONDecodeError, AttributeError) as exc:
                raise JournalError("lease journal contains an unreadable record") from exc
        return [r for r in records if r is not None]

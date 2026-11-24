"""Raw event recorder and storage quota manager (spec p.6, p.40–41, p.111, p.114).

Raw schema: venue, channel, market_ticker, sequence, exchange_ts, receive_ts, monotonic_ns, payload,
schema_version, connection_id. Written as Zstandard-compressed Parquet partitioned by UTC date and venue,
via atomic temp-file rename, with a SHA-256 manifest. Corrupted partitions are rejected on read.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

SCHEMA_VERSION = 1
RAW_SCHEMA = pa.schema([
    ("venue", pa.string()), ("channel", pa.string()), ("market_ticker", pa.string()), ("sequence", pa.int64()),
    ("exchange_ts", pa.float64()), ("receive_ts", pa.float64()), ("monotonic_ns", pa.int64()),
    ("payload", pa.string()), ("schema_version", pa.int32()), ("connection_id", pa.int32()),
])


class StorageError(RuntimeError):
    pass


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Recorder:
    root: Path
    flush_rows: int = 5000
    flush_seconds: float = 10.0
    buffers: dict[tuple[str, str], list[dict]] = field(default_factory=dict)
    last_flush: float = field(default_factory=time.monotonic)
    enabled: bool = True
    bytes_written: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        (self.root / "raw").mkdir(parents=True, exist_ok=True)

    def record(self, venue: str, channel: str, payload: Any, receive_ts: float, ticker: str = "", seq: int | None = None,
               exchange_ts: float | None = None, connection_id: int = 0) -> None:
        if not self.enabled:
            return
        day = datetime.fromtimestamp(receive_ts, timezone.utc).strftime("%Y-%m-%d")
        row = {"venue": venue, "channel": channel, "market_ticker": ticker, "sequence": seq, "exchange_ts": exchange_ts,
               "receive_ts": receive_ts, "monotonic_ns": time.monotonic_ns(),
               "payload": payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":")),
               "schema_version": SCHEMA_VERSION, "connection_id": connection_id}
        with self._lock:
            self.buffers.setdefault((day, venue), []).append(row)
            n = sum(len(v) for v in self.buffers.values())
        if n >= self.flush_rows or time.monotonic() - self.last_flush > self.flush_seconds:
            self.flush()

    def flush(self) -> list[Path]:
        with self._lock:
            bufs, self.buffers = self.buffers, {}
            self.last_flush = time.monotonic()
        written = []
        for (day, venue), rows in bufs.items():
            if not rows:
                continue
            d = self.root / "raw" / f"date={day}" / f"venue={venue}"
            d.mkdir(parents=True, exist_ok=True)
            name = f"part-{int(time.time() * 1000)}-{os.getpid()}.parquet"
            tmp = d / (name + ".tmp")
            table = pa.Table.from_pylist(rows, schema=RAW_SCHEMA)
            try:
                pq.write_table(table, tmp, compression="zstd")
                os.replace(tmp, d / name)  # atomic
            except OSError as exc:
                tmp.unlink(missing_ok=True)
                raise StorageError(f"write failure: {exc}") from exc
            digest = sha256_file(d / name)
            with open(d / "MANIFEST.jsonl", "a") as m:
                m.write(json.dumps({"file": name, "sha256": digest, "rows": len(rows)}) + "\n")
                m.flush()
                os.fsync(m.fileno())
            self.bytes_written += (d / name).stat().st_size
            written.append(d / name)
        return written


def read_partition(d: Path) -> pa.Table:
    """Read a partition verifying every file against the manifest; corrupted data is rejected (p.6)."""
    manifest = {}
    mf = d / "MANIFEST.jsonl"
    if mf.exists():
        for line in mf.read_text().splitlines():
            if line.strip():
                e = json.loads(line)
                manifest[e["file"]] = e["sha256"]
    tables = []
    for f in sorted(d.glob("*.parquet")):
        if f.name not in manifest:
            raise StorageError(f"unmanifested file {f.name}")
        if sha256_file(f) != manifest[f.name]:
            raise StorageError(f"checksum mismatch {f.name}")
        tables.append(pq.ParquetFile(f).read())
    return pa.concat_tables(tables) if tables else RAW_SCHEMA.empty_table()


# ------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PressureAction:
    level: str  # ok | rotate | prune | pause_ml | halt_entry
    usage: float
    projected_days: float | None


def pressure(total_bytes: int, free_bytes: int, daily_growth_bytes: float | None, days_to_cleanup: float = 1.0) -> PressureAction:
    """Thresholds 60/70/80/90 % (p.41) using the more conservative of usage and projected exhaustion."""
    used = total_bytes - free_bytes
    usage = used / total_bytes if total_bytes else 1.0
    proj = (free_bytes / daily_growth_bytes) if daily_growth_bytes and daily_growth_bytes > 0 else None
    if usage >= 0.90:
        lvl = "halt_entry"
    elif usage >= 0.80:
        lvl = "pause_ml"
    elif usage >= 0.70:
        lvl = "prune"
    elif usage >= 0.60:
        lvl = "rotate"
    else:
        lvl = "ok"
    if proj is not None and proj < days_to_cleanup and lvl in ("ok", "rotate"):
        lvl = "prune"  # earlier intervention if exhaustion precedes next cleanup
    return PressureAction(lvl, usage, proj)


def disk_status(path: str | Path) -> tuple[int, int]:
    u = shutil.disk_usage(path)
    return u.total, u.free


def prune_raw(root: Path, retention_hours: int, now: float, protect: set[str] | None = None) -> list[str]:
    """Delete oldest raw partitions beyond retention first. Ledger/models/unsettled records live elsewhere
    and are never touched here."""
    removed = []
    raw = Path(root) / "raw"
    cutoff = datetime.fromtimestamp(now - retention_hours * 3600, timezone.utc).strftime("%Y-%m-%d")
    for d in sorted(raw.glob("date=*")):
        day = d.name.split("=", 1)[1]
        if day < cutoff and day not in (protect or set()):
            shutil.rmtree(d)
            removed.append(day)
    return removed


@dataclass
class FeatureRecorder:
    """One-second derived feature rows for research (p.6 derived schema). Zstd Parquet by UTC date."""

    root: Path
    flush_rows: int = 600
    rows: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    def add(self, row: dict) -> None:
        self.rows.append(row)
        if len(self.rows) >= self.flush_rows:
            self.flush()

    def flush(self) -> None:
        if not self.rows:
            return
        rows, self.rows = self.rows, []
        day = datetime.fromtimestamp(rows[0]["ts"], timezone.utc).strftime("%Y-%m-%d")
        d = self.root / f"date={day}"
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"part-{int(time.time() * 1000)}.parquet.tmp"
        pq.write_table(pa.Table.from_pylist(rows), tmp, compression="zstd")
        os.replace(tmp, tmp.with_suffix(""))

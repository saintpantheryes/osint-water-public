"""文件式存储:原始响应落盘(可重放)、按期 parquet 快照、run_log。

data/raw/{source}/{YYYY-MM-DD}/{name}.json.gz   原始响应,永不修改
data/snapshots/{YYYY-MM-DD}/{table}.parquet      归一后、过滤前的全集
data/run_log.jsonl                               每次采集一行
"""
from __future__ import annotations

import gzip
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

DATA_DIR = Path(os.environ.get("OSINT_WATER_DATA", Path(__file__).resolve().parent.parent / "data"))
# 抓取时如实报上身份。自己部署的请在 worker.env 里设 OW_USER_AGENT,写上自己的项目地址或联系方式,
# 信源方有问题能找到真正运行它的人
USER_AGENT = os.environ.get("OW_USER_AGENT") or "osint-water/0.1 (+https://github.com/saintpantheryes/osint-water-public)"


def today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def write_raw(source: str, name: str, payload: object, day: str | None = None) -> Path:
    d = DATA_DIR / "raw" / source / (day or today())
    d.mkdir(parents=True, exist_ok=True)
    fp = d / f"{name}.json.gz"
    with gzip.open(fp, "wt", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return fp


def write_snapshot(table: str, rows: list[dict], day: str | None = None) -> Path:
    """写入当日快照;同日重跑覆盖。空表也写(保留 schema 由调用方保证至少有列)。"""
    d = DATA_DIR / "snapshots" / (day or today())
    d.mkdir(parents=True, exist_ok=True)
    fp = d / f"{table}.parquet"
    pq.write_table(pa.Table.from_pylist(rows), fp, compression="snappy")  # hyparquet 不支持 zstd
    return fp


def latest_snapshot(table: str, before: str | None = None) -> Path | None:
    """最近一期快照(可排除当日,用于两期对比)。"""
    root = DATA_DIR / "snapshots"
    if not root.exists():
        return None
    days = sorted(p.name for p in root.iterdir() if p.is_dir() and (p / f"{table}.parquet").exists())
    if before:
        days = [d for d in days if d < before]
    return (root / days[-1] / f"{table}.parquet") if days else None


def log_run(source: str, status: str, rows: int, started_at: str, note: str = "") -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"source": source, "status": status, "rows": rows, "started_at": started_at, "finished_at": now_iso(), "note": note}
    with (DATA_DIR / "run_log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

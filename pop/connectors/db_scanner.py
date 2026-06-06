"""
pop.connectors.db_scanner
~~~~~~~~~~~~~~~~~~~~~~~~~
Initiates a metadata scan on the database(s) backing an application CI
and returns the values the POP scoring engine needs:

    record_count           — total rows across relevant tables
    data_gb                — total storage footprint
    oldest_record_age_days — age of the oldest data subject record
    scanned_at             — ISO timestamp of the scan

Supported database types (configured per-app in config.yaml or CMDB):
    mock          Synthetic deterministic data — no real connection needed.
    sqlite        Standard library sqlite3.
    postgresql    Requires psycopg2 (pip install psycopg2-binary).
    mysql         Requires mysql-connector-python.
    mssql         Requires pyodbc.

The scanner is intentionally read-only — it only runs SELECT/metadata queries.
Credentials are resolved from environment variables; never stored in config.

To add a new database type, add an entry to _SCANNERS mapping a type string
to a function with signature:
    (connection_info: dict, scan_cfg: dict) -> DBScanResult
No other code changes required.
"""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class DBScanResult:
    record_count: int
    data_gb: float
    oldest_record_age_days: int
    scanned_at: str
    db_type: str
    warnings: list[str] = field(default_factory=list)


# ── Mock scanner ──────────────────────────────────────────────────────────────

def _scan_mock(connection_info: dict, scan_cfg: dict) -> DBScanResult:
    """
    Deterministic synthetic scan — derived from app_id hash so each app
    gets consistent, varied metadata across runs.  No real DB connection.
    """
    seed = int(hashlib.md5(connection_info.get("app_id", "x").encode()).hexdigest(), 16)

    # Spread results across realistic ranges using the hash
    record_count = int(1000 + (seed % 9_900_000))          # 1K – 10M
    data_gb      = round(0.1 + (seed % 9000) / 10.0, 1)    # 0.1 – 900 GB
    oldest_days  = int(180 + (seed % 5000))                 # 6 months – ~14 years

    return DBScanResult(
        record_count=record_count,
        data_gb=data_gb,
        oldest_record_age_days=oldest_days,
        scanned_at=datetime.now(timezone.utc).isoformat(),
        db_type="mock",
    )


# ── SQLite scanner ────────────────────────────────────────────────────────────

def _scan_sqlite(connection_info: dict, scan_cfg: dict) -> DBScanResult:
    import sqlite3

    db_path = connection_info["path"]
    warnings: list[str] = []

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()

        # Total row count across all tables
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        record_count = 0
        for table in tables:
            try:
                cursor.execute(f"SELECT COUNT(*) FROM \"{table}\"")  # noqa: S608
                record_count += cursor.fetchone()[0]
            except Exception:
                warnings.append(f"Could not count rows in table '{table}'")

        # Storage size via page_count * page_size
        cursor.execute("PRAGMA page_count")
        page_count = cursor.fetchone()[0]
        cursor.execute("PRAGMA page_size")
        page_size = cursor.fetchone()[0]
        data_gb = round((page_count * page_size) / (1024 ** 3), 4)

        # Oldest record: scan configured date columns
        date_columns = scan_cfg.get("date_columns", [])
        oldest_ts: datetime | None = None
        for entry in date_columns:
            try:
                cursor.execute(
                    f"SELECT MIN(\"{entry['column']}\") FROM \"{entry['table']}\""  # noqa: S608
                )
                val = cursor.fetchone()[0]
                if val:
                    ts = datetime.fromisoformat(str(val))
                    if oldest_ts is None or ts < oldest_ts:
                        oldest_ts = ts
            except Exception:
                warnings.append(f"Could not read date column {entry}")

        if oldest_ts:
            oldest_days = (datetime.now(timezone.utc) - oldest_ts.replace(tzinfo=timezone.utc)).days
        else:
            oldest_days = 0
            warnings.append("No date columns configured — oldest_record_age_days set to 0")

        return DBScanResult(
            record_count=record_count,
            data_gb=data_gb,
            oldest_record_age_days=oldest_days,
            scanned_at=datetime.now(timezone.utc).isoformat(),
            db_type="sqlite",
            warnings=warnings,
        )
    finally:
        conn.close()


# ── PostgreSQL scanner ────────────────────────────────────────────────────────

def _scan_postgresql(connection_info: dict, scan_cfg: dict) -> DBScanResult:
    try:
        import psycopg2
    except ImportError:
        raise RuntimeError("psycopg2 is required for PostgreSQL scanning: pip install psycopg2-binary")

    host     = connection_info["host"]
    port     = int(connection_info.get("port", 5432))
    dbname   = connection_info["database"]
    user     = _resolve_env(connection_info.get("username_env", ""))
    password = _resolve_env(connection_info.get("password_env", ""))
    warnings: list[str] = []

    conn = psycopg2.connect(host=host, port=port, dbname=dbname, user=user, password=password,
                            connect_timeout=scan_cfg.get("timeout_seconds", 30))
    try:
        cur = conn.cursor()

        # Total row count from pg_stat_user_tables (fast — uses stats)
        cur.execute("""
            SELECT COALESCE(SUM(n_live_tup), 0)
            FROM pg_stat_user_tables
        """)
        record_count = int(cur.fetchone()[0])

        # Storage size
        cur.execute("SELECT pg_database_size(current_database())")
        size_bytes = cur.fetchone()[0]
        data_gb = round(size_bytes / (1024 ** 3), 4)

        # Oldest record
        date_columns = scan_cfg.get("date_columns", [])
        oldest_ts: datetime | None = None
        for entry in date_columns:
            try:
                cur.execute(
                    f'SELECT MIN("{entry["column"]}") FROM "{entry["table"]}"'  # noqa: S608
                )
                val = cur.fetchone()[0]
                if val and isinstance(val, datetime):
                    if oldest_ts is None or val < oldest_ts:
                        oldest_ts = val
            except Exception:
                warnings.append(f"Could not read date column {entry}")

        oldest_days = 0
        if oldest_ts:
            ots = oldest_ts.replace(tzinfo=timezone.utc) if oldest_ts.tzinfo is None else oldest_ts
            oldest_days = (datetime.now(timezone.utc) - ots).days

        return DBScanResult(
            record_count=record_count,
            data_gb=data_gb,
            oldest_record_age_days=oldest_days,
            scanned_at=datetime.now(timezone.utc).isoformat(),
            db_type="postgresql",
            warnings=warnings,
        )
    finally:
        conn.close()


# ── MySQL scanner ─────────────────────────────────────────────────────────────

def _scan_mysql(connection_info: dict, scan_cfg: dict) -> DBScanResult:
    try:
        import mysql.connector
    except ImportError:
        raise RuntimeError("mysql-connector-python is required: pip install mysql-connector-python")

    warnings: list[str] = []
    conn = mysql.connector.connect(
        host=connection_info["host"],
        port=int(connection_info.get("port", 3306)),
        database=connection_info["database"],
        user=_resolve_env(connection_info.get("username_env", "")),
        password=_resolve_env(connection_info.get("password_env", "")),
        connection_timeout=scan_cfg.get("timeout_seconds", 30),
    )
    try:
        cur = conn.cursor()
        db = connection_info["database"]

        cur.execute("""
            SELECT COALESCE(SUM(TABLE_ROWS), 0),
                   COALESCE(SUM(DATA_LENGTH + INDEX_LENGTH), 0)
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %s
        """, (db,))
        row = cur.fetchone()
        record_count = int(row[0])
        data_gb = round(int(row[1]) / (1024 ** 3), 4)

        date_columns = scan_cfg.get("date_columns", [])
        oldest_ts: datetime | None = None
        for entry in date_columns:
            try:
                cur.execute(f"SELECT MIN(`{entry['column']}`) FROM `{entry['table']}`")  # noqa: S608
                val = cur.fetchone()[0]
                if val and isinstance(val, datetime):
                    if oldest_ts is None or val < oldest_ts:
                        oldest_ts = val
            except Exception:
                warnings.append(f"Could not read date column {entry}")

        oldest_days = 0
        if oldest_ts:
            ots = oldest_ts.replace(tzinfo=timezone.utc) if oldest_ts.tzinfo is None else oldest_ts
            oldest_days = (datetime.now(timezone.utc) - ots).days

        return DBScanResult(
            record_count=record_count,
            data_gb=data_gb,
            oldest_record_age_days=oldest_days,
            scanned_at=datetime.now(timezone.utc).isoformat(),
            db_type="mysql",
            warnings=warnings,
        )
    finally:
        conn.close()


# ── Dispatcher ────────────────────────────────────────────────────────────────

_SCANNERS: dict[str, Callable] = {
    "mock":       _scan_mock,
    "sqlite":     _scan_sqlite,
    "postgresql": _scan_postgresql,
    "mysql":      _scan_mysql,
}


def _resolve_env(value: str) -> str:
    if value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value


class DBScanner:
    """
    Facade that dispatches to the right scanner implementation based on
    ``connection_info["type"]``.  New database types can be registered by
    adding to _SCANNERS without changing this class.
    """

    def __init__(self, scan_cfg: dict[str, Any]):
        self.scan_cfg = scan_cfg

    def scan(self, connection_info: dict[str, Any]) -> DBScanResult:
        """
        Initiate a metadata scan for the given connection.
        ``connection_info`` comes from the CMDB CI or config.yaml.
        """
        db_type = connection_info.get("type", "mock").lower()
        scanner_fn = _SCANNERS.get(db_type)
        if scanner_fn is None:
            return DBScanResult(
                record_count=0,
                data_gb=0.0,
                oldest_record_age_days=0,
                scanned_at=datetime.now(timezone.utc).isoformat(),
                db_type=db_type,
                warnings=[f"Unsupported DB type '{db_type}'. Supported: {list(_SCANNERS)}."],
            )
        return scanner_fn(connection_info, self.scan_cfg)

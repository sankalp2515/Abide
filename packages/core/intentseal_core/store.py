"""Persistence: SQLite by default, PostgreSQL for shared or multi-instance deployments (API-12).

`Store(target)` opens SQLite for a file path (or ":memory:") and PostgreSQL for a `postgresql://` URL
(INTENTSEAL_DATABASE_URL). Application SQL is written once, in a portable subset (`?` placeholders,
`ON CONFLICT` upserts); the PostgreSQL store adapts placeholders and table definitions in one place.

This module owns LLM usage and the response cache. Decision/event tables are added by the decision
logger (CORE-10) through `Store.ensure_schema`.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from .secrets import redact, registry

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS llm_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        role TEXT, provider TEXT, key_alias TEXT, model TEXT,
        status TEXT NOT NULL,            -- ok | rate_limited | auth_failed | server_error | timeout | bad_request | not_found | error | cached
        http_status INTEGER,
        latency_ms REAL,
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        est_cost_usd REAL DEFAULT 0,
        cached INTEGER DEFAULT 0,
        error TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_llm_usage_ts ON llm_usage(ts)",
    "CREATE INDEX IF NOT EXISTS ix_llm_usage_provider ON llm_usage(provider, ts)",
    """CREATE TABLE IF NOT EXISTS llm_cache (
        cache_key TEXT PRIMARY KEY,
        payload TEXT NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL
    )""",
]

POSTGRES_PREFIXES = ("postgres://", "postgresql://")


def is_database_url(target: object) -> bool:
    return str(target).startswith(POSTGRES_PREFIXES)


class Store:
    backend = "sqlite"

    def __new__(cls, path: Path | str = ":memory:", *args: Any, **kwargs: Any) -> "Store":
        if cls is Store and is_database_url(path):
            return super().__new__(PostgresStore)
        return super().__new__(cls)

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.row_factory = sqlite3.Row
        self.ensure_schema(_SCHEMA)

    def ensure_schema(self, statements: list[str]) -> None:
        with self._lock:
            for s in statements:
                self._conn.execute(s)

    def execute(self, sql: str, params: tuple | list = ()) -> Any:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def ping(self) -> bool:
        """Readiness: the database answers."""
        try:
            return self.query("SELECT 1 AS ok")[0]["ok"] == 1
        except Exception:
            return False

    def describe(self) -> str:
        return f"sqlite:{self.path}"

    # ---- LLM usage -------------------------------------------------------------------------
    def record_usage(self, **row: Any) -> None:
        if row.get("error"):
            row["error"] = redact(str(row["error"]))[:500]
        row.setdefault("ts", time.time())
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self.execute(f"INSERT INTO llm_usage ({cols}) VALUES ({marks})", tuple(row.values()))

    def tokens_today(self, provider: str) -> int:
        start = time.time() - (time.time() % 86400)  # UTC day
        r = self.query(
            "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS t FROM llm_usage "
            "WHERE provider = ? AND ts >= ? AND cached = 0",
            (provider, start),
        )
        return int(r[0]["t"])

    # ---- Response cache --------------------------------------------------------------------
    def cache_get(self, key: str) -> dict[str, Any] | None:
        r = self.query("SELECT payload, expires_at FROM llm_cache WHERE cache_key = ?", (key,))
        if not r or r[0]["expires_at"] < time.time():
            return None
        return json.loads(r[0]["payload"])

    def cache_put(self, key: str, payload: dict[str, Any], ttl_s: int) -> None:
        now = time.time()
        self.execute(
            "INSERT INTO llm_cache (cache_key, payload, created_at, expires_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (cache_key) DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at, "
            "expires_at = excluded.expires_at",
            (key, json.dumps(payload), now, now + ttl_s),
        )


_DDL = [(re.compile(r"INTEGER PRIMARY KEY AUTOINCREMENT", re.I), "BIGSERIAL PRIMARY KEY"),
        (re.compile(r"\bREAL\b"), "DOUBLE PRECISION")]


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):   # SUM() over integers is numeric in PostgreSQL
        return int(value) if value == value.to_integral_value() else float(value)
    return value


class PostgresStore(Store):
    """PostgreSQL behind the same interface (psycopg 3, autocommit, one connection guarded by a lock and
    reopened once if it drops). Install with `pip install intentseal-core[postgres]`."""

    backend = "postgres"

    def __init__(self, url: str) -> None:   # noqa: D107 - same contract as Store
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as e:   # pragma: no cover - depends on the optional extra
            raise RuntimeError("PostgreSQL needs the optional dependency: pip install 'intentseal-core[postgres]'") from e
        self._psycopg, self._dict_row = psycopg, dict_row
        self.url = str(url)
        m = re.match(r"^[a-z]+://[^:/@]+:([^@]+)@", self.url)
        if m:
            registry.register(m.group(1))   # the password never reaches a log line
        self.path = Path("postgres")
        self._lock = threading.Lock()
        self._conn = self._connect()
        self.ensure_schema(_SCHEMA)

    def _connect(self) -> Any:
        return self._psycopg.connect(self.url, autocommit=True, row_factory=self._dict_row, connect_timeout=10)

    @staticmethod
    def _sql(sql: str) -> str:
        return sql.replace("%", "%%").replace("?", "%s")

    def _run(self, sql: str, params: tuple | list, fetch: bool) -> Any:
        with self._lock:
            for attempt in (1, 2):
                try:
                    cur = self._conn.execute(self._sql(sql), tuple(params))
                    return [{k: _plain(v) for k, v in r.items()} for r in cur.fetchall()] if fetch else cur
                except self._psycopg.OperationalError:
                    if attempt == 2 or not self._conn.closed:
                        raise
                    self._conn = self._connect()   # connection dropped: reopen once

    def ensure_schema(self, statements: list[str]) -> None:
        for s in statements:
            for pattern, repl in _DDL:
                s = pattern.sub(repl, s)
            self._run(s, (), fetch=False)

    def execute(self, sql: str, params: tuple | list = ()) -> Any:
        return self._run(sql, params, fetch=False)

    def query(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        return self._run(sql, params, fetch=True)

    def describe(self) -> str:
        return "postgres:" + re.sub(r"//[^@/]*@", "//***@", self.url)

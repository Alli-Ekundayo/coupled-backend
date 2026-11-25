"""Lakebase Postgres connection management and SQL migrations for coupled-backend.

Best practices from Neon Python / Postgres guidance:
- Native Python driver: psycopg 3 + psycopg_pool
- Direct connection (DATABASE_URL_UNPOOLED) for schema migrations / DDL
- Pooled connection (DATABASE_URL, -pooler host) for normal application traffic
- prepare_threshold=None for PgBouncer transaction pooling compatibility
- min_size=0 + bounded max_idle so Neon compute can scale to zero when idle
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_lock = threading.Lock()
_pool: ConnectionPool | None = None
_pool_dsn: str = ""
_migrated_dsn: str = ""


def is_postgres_enabled() -> bool:
    return bool(config.DATABASE_URL)


def direct_dsn() -> str:
    """Return direct (unpooled) connection string for migrations & DDL."""
    return config.DATABASE_URL_UNPOOLED or config.DATABASE_URL


def run_migrations(dsn: str | None = None) -> list[str]:
    """Apply versioned SQL migrations over a direct (unpooled) Postgres connection."""
    target = dsn or direct_dsn()
    if not target:
        return []

    applied: list[str] = []
    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migration_files:
        return applied

    with psycopg.connect(target, autocommit=False) as conn:
        with conn.cursor() as cur:
            # Advisory lock prevents concurrent startup races across workers
            cur.execute("SELECT pg_advisory_lock(hashtext('coupled_backend_schema_migrations'))")
            try:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version TEXT PRIMARY KEY,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                conn.commit()

                cur.execute("SELECT version FROM schema_migrations")
                existing = {row[0] for row in cur.fetchall()}

                for path in migration_files:
                    version = path.name
                    if version in existing:
                        continue
                    sql = path.read_text(encoding="utf-8")
                    cur.execute(sql)
                    cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                    conn.commit()
                    applied.append(version)
                    logger.info("Applied Postgres migration %s", version)
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.execute("SELECT pg_advisory_unlock(hashtext('coupled_backend_schema_migrations'))")
                conn.commit()
    return applied


def get_pool() -> ConnectionPool:
    """Return the shared psycopg 3 pool pointing at Neon's pooled DATABASE_URL."""
    global _pool, _pool_dsn
    dsn = config.DATABASE_URL
    if not dsn:
        raise RuntimeError("DATABASE_URL is not configured")
    with _lock:
        if _pool is not None and _pool_dsn != dsn:
            try:
                _pool.close()
            except Exception:
                pass
            _pool = None
            _pool_dsn = ""
        if _pool is None:
            _pool = ConnectionPool(
                conninfo=dsn,
                min_size=0,
                max_size=8,
                max_idle=120.0,
                timeout=60.0,
                kwargs={"prepare_threshold": None, "row_factory": dict_row, "connect_timeout": 45},
                open=True,
            )
            _pool_dsn = dsn
        return _pool


def init_db() -> None:
    """Run migrations (via direct URL) and prepare pooled connection pool."""
    global _migrated_dsn
    if not is_postgres_enabled():
        return
    with _lock:
        ddsn = direct_dsn()
        if _migrated_dsn != ddsn:
            run_migrations(ddsn)
            _migrated_dsn = ddsn
    get_pool()


def close_db() -> None:
    """Close the Postgres connection pool cleanly on shutdown."""
    global _pool, _pool_dsn
    with _lock:
        if _pool is not None:
            try:
                _pool.close()
            finally:
                _pool = None
                _pool_dsn = ""


def health_check() -> dict[str, Any]:
    """Verify live database connectivity and report Postgres/SQLite status."""
    if not is_postgres_enabled():
        return {
            "backend": "sqlite",
            "connected": True,
            "path": str(config.DB_PATH),
        }
    try:
        pool = get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database() AS db, version() AS version")
                row = cur.fetchone() or {}
        return {
            "backend": "postgres",
            "connected": True,
            "pooled": "-pooler" in config.DATABASE_URL,
            "branch": config.NEON_BRANCH or None,
            "branch_id": config.NEON_BRANCH_ID or None,
            "database": row.get("db"),
            "postgres_version": row.get("version"),
        }
    except Exception as exc:
        logger.warning("Postgres health check failed: %s", exc)
        return {
            "backend": "postgres",
            "connected": False,
            "error": str(exc),
        }

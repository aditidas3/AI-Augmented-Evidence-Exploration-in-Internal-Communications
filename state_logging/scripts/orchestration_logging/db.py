"""
db.py — Database connection pool.

Single shared pool used by the worker thread.
The main agent thread never touches this directly.
"""

import os
import psycopg2
from psycopg2 import pool


_pool = None #_pool: pool.ThreadedConnectionPool | None = None


def init_pool(
    host: str     = None,
    port: int     = None,
    dbname: str   = None,
    user: str     = None,
    password: str = None,
    min_conn: int = 1,
    max_conn: int = 5,
) -> None:
    """
    Call once at application startup before any logging happens.

    Falls back to environment variables if parameters are not provided:
        AEX_DB_HOST, AEX_DB_PORT, AEX_DB_NAME, AEX_DB_USER, AEX_DB_PASSWORD
    """
    global _pool
    _pool = pool.ThreadedConnectionPool(
        minconn=min_conn,
        maxconn=max_conn,
        host     = host     or os.environ.get("AEX_DB_HOST",     "localhost"),
        port     = port     or int(os.environ.get("AEX_DB_PORT", "5432")),
        dbname   = dbname   or os.environ.get("AEX_DB_NAME",     "aex_test"),
        user     = user     or os.environ.get("AEX_DB_USER",     "postgres"),
        password = password or os.environ.get("AEX_DB_PASSWORD", "postgresql123"),
    )


def get_conn():
    """Borrow a connection from the pool."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call db.init_pool() first.")
    return _pool.getconn()


def put_conn(conn) -> None:
    """Return a connection to the pool."""
    if _pool is not None:
        _pool.putconn(conn)


def close_pool() -> None:
    """Call at application shutdown."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None

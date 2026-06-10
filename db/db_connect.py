from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row


ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(ENV_PATH, override=False)


class PostgresClient:
    """Small helper around psycopg for common PostgreSQL operations."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        dbname: str,
        user: str,
        password: str,
        sslmode: str = "prefer",
        connect_timeout: int = 10,
    ) -> None:
        self.host = host
        self.port = port
        self.dbname = dbname
        self.user = user
        self.password = password
        self.sslmode = sslmode
        # libpq's default connect timeout is unbounded, which means a
        # VPN drop or DNS failure stalls callers for the full TCP
        # SYN retry window (~130 s on most platforms). Default to
        # 10 s so ``PostgresLexicalIndex.try_create`` can fall back
        # to Cypher-only retrieval quickly. Override via
        # ``POSTGRES_CONNECT_TIMEOUT`` when running long batch jobs.
        self.connect_timeout = int(connect_timeout)
        self._conn: psycopg.Connection[Any] | None = None

    @classmethod
    def from_env(cls) -> "PostgresClient":
        host = os.getenv("POSTGRES_HOST", "localhost").strip()
        port = int(os.getenv("POSTGRES_PORT", "5432"))
        dbname = os.getenv("POSTGRES_DB", "").strip()
        user = os.getenv("POSTGRES_USER", "").strip()
        password = os.getenv("POSTGRES_PASSWORD", "").strip()
        sslmode = os.getenv("POSTGRES_SSLMODE", "prefer").strip() or "prefer"
        connect_timeout = int(os.getenv("POSTGRES_CONNECT_TIMEOUT", "10"))

        missing = [
            key
            for key, value in {
                "POSTGRES_DB": dbname,
                "POSTGRES_USER": user,
                "POSTGRES_PASSWORD": password,
            }.items()
            if not value
        ]
        if missing:
            missing_list = ", ".join(missing)
            raise RuntimeError(f"Missing PostgreSQL env vars in root .env: {missing_list}")

        return cls(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
            sslmode=sslmode,
            connect_timeout=connect_timeout,
        )

    def dsn(self) -> str:
        return (
            f"host={self.host} "
            f"port={self.port} "
            f"dbname={self.dbname} "
            f"user={self.user} "
            f"password={self.password} "
            f"sslmode={self.sslmode} "
            f"connect_timeout={self.connect_timeout}"
        )

    def _get_connection(self) -> psycopg.Connection[Any]:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn(), row_factory=dict_row)
        return self._conn

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[Any]]:
        yield self._get_connection()

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def test_connection(self) -> dict[str, Any]:
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database() AS db, current_user AS user, version() AS version;")
                row = cur.fetchone()
        return dict(row) if row else {}

    def fetch_all(
        self,
        query: str,
        params: Sequence[Any] | dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
        return [dict(row) for row in rows]

    def fetch_one(
        self,
        query: str,
        params: Sequence[Any] | dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                row = cur.fetchone()
        return dict(row) if row else None

    def execute(
        self,
        query: str,
        params: Sequence[Any] | dict[str, Any] | None = None,
    ) -> int:
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                affected = cur.rowcount
            conn.commit()
        return affected

    def execute_many(self, query: str, params_seq: Sequence[Sequence[Any] | dict[str, Any]]) -> None:
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(query, params_seq)
            conn.commit()


def get_postgres_client() -> PostgresClient:
    return PostgresClient.from_env()


if __name__ == "__main__":
    client = get_postgres_client()
    result = client.test_connection()
    print("PostgreSQL connected successfully.")
    print(result)

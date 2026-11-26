"""PostgreSQL persistence. Connection settings come from the standard libpq
environment variables (PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD)."""

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    code       varchar(16) PRIMARY KEY,
    url        text        NOT NULL,
    hits       bigint      NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
)
"""


class CodeTaken(Exception):
    pass


class PgStore:
    def __init__(self, conninfo: str = ""):
        self.pool = ConnectionPool(conninfo, min_size=1, max_size=8, open=False, kwargs={"row_factory": dict_row})
        self.pool.open(wait=False)
        self.schema_ready = False

    def ensure_schema(self):
        if not self.schema_ready:
            with self.pool.connection() as conn:
                conn.execute(SCHEMA)
            self.schema_ready = True

    def ping(self):
        self.ensure_schema()
        with self.pool.connection(timeout=2) as conn:
            conn.execute("SELECT 1")

    def create(self, code: str, url: str):
        try:
            with self.pool.connection() as conn:
                conn.execute("INSERT INTO links (code, url) VALUES (%s, %s)", (code, url))
        except UniqueViolation as exc:
            raise CodeTaken(code) from exc

    def hit(self, code: str):
        with self.pool.connection() as conn:
            row = conn.execute("UPDATE links SET hits = hits + 1 WHERE code = %s RETURNING url", (code,)).fetchone()
        return row["url"] if row else None

    def get(self, code: str):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT code, url, hits, created_at FROM links WHERE code = %s", (code,)).fetchone()
        if row:
            row["created_at"] = row["created_at"].isoformat()
        return row

    def delete(self, code: str) -> bool:
        with self.pool.connection() as conn:
            return conn.execute("DELETE FROM links WHERE code = %s", (code,)).rowcount == 1

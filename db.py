import os
from contextlib import contextmanager

import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]
DB_HOST = os.environ["DB_HOST"]
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_SSLMODE = os.environ.get("DB_SSLMODE", "prefer")

# ThreadedConnectionPool, not SimpleConnectionPool: FastMCP runs sync tool
# functions via anyio.to_thread.run_sync, so concurrent requests genuinely
# hit this pool from multiple OS threads. SimpleConnectionPool's internal
# free-list isn't safe for that.
_pool = ThreadedConnectionPool(
    1, 20, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD, host=DB_HOST,
    port=DB_PORT, sslmode=DB_SSLMODE,
)


@contextmanager
def get_cursor(commit=False):
    """Checkout a pooled connection/cursor. Pass commit=True for writes;
    reads are rolled back on exit so no idle-in-transaction connections pile up."""
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit() if commit else conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)

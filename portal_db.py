import os
import threading

import psycopg2
import psycopg2.pool

_pool = None
_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    1, 10,
                    host=os.environ.get("DB_HOST", "localhost"),
                    port=int(os.environ.get("DB_PORT", "5432")),
                    dbname=os.environ.get("DB_NAME", "botdb"),
                    user=os.environ.get("DB_USER", "botuser"),
                    password=os.environ.get("DB_PASS", ""),
                )
    return _pool


def query_one(sql, params=()):
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
    finally:
        pool.putconn(conn)


def query_all(sql, params=()):
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        pool.putconn(conn)


def execute(sql, params=()):
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            if cur.description:
                row = cur.fetchone()
                if row:
                    cols = [d[0] for d in cur.description]
                    return dict(zip(cols, row))
            return None
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)

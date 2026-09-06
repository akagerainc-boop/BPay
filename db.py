"""MySQL access for the BPay backend (XAMPP-friendly)."""

import os
from contextlib import contextmanager

import mysql.connector
from mysql.connector import pooling

_pool = None


def _config():
    return {
        "host": os.getenv("DB_HOST", "127.0.0.1"),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.getenv("DB_USER", "root"),
        "password": os.getenv("DB_PASSWORD", ""),
        "database": os.getenv("DB_NAME", "bpay"),
    }


def init_pool():
    """Creates the connection pool once, lazily."""
    global _pool
    if _pool is None:
        _pool = pooling.MySQLConnectionPool(
            pool_name="bpay_pool", pool_size=5, **_config()
        )
    return _pool


@contextmanager
def get_cursor(dictionary=True, commit=False):
    """Yields a cursor, always returning the connection to the pool."""
    conn = init_pool().get_connection()
    cursor = conn.cursor(dictionary=dictionary)
    try:
        yield cursor
        if commit:
            conn.commit()
    except mysql.connector.Error:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def query_all(sql, params=None):
    with get_cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


def query_one(sql, params=None):
    with get_cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchone()


def execute(sql, params=None):
    """Runs a write and returns lastrowid (or affected rows for updates)."""
    with get_cursor(commit=True) as cur:
        cur.execute(sql, params or ())
        return cur.lastrowid or cur.rowcount

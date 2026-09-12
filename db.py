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


def ensure_payment_link_schema():
    """Creates the payment-link tables/column if this database predates
    them. Safe to call on every startup — `CREATE TABLE IF NOT EXISTS` is
    a no-op once applied, and the ALTER is guarded against the "duplicate
    column" error a second run would otherwise hit. There's no separate
    migration runner for this database, so app startup is where a schema
    change like this actually reaches production."""
    with get_cursor(commit=True) as cur:
        cur.execute(
            """CREATE TABLE IF NOT EXISTS payment_links (
                 id                INT AUTO_INCREMENT PRIMARY KEY,
                 code              VARCHAR(16)  NOT NULL UNIQUE,
                 device_id         VARCHAR(128) NOT NULL,
                 owner_phone       VARCHAR(20)  NULL,
                 owner_network     ENUM('mtn','airtel') NULL,
                 destination       VARCHAR(120) NOT NULL,
                 destination_type  ENUM('phone','merchant') NOT NULL,
                 network           ENUM('mtn','airtel','unknown') NOT NULL DEFAULT 'unknown',
                 amount            INT          NOT NULL,
                 status            ENUM('active','paused','deleted') NOT NULL DEFAULT 'active',
                 use_count         INT          NOT NULL DEFAULT 0,
                 fee_charges_done  INT          NOT NULL DEFAULT 0,
                 created_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
                 updated_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                                 ON UPDATE CURRENT_TIMESTAMP,
                 INDEX idx_device (device_id),
                 INDEX idx_status (status)
               ) ENGINE=InnoDB"""
        )
        cur.execute(
            """CREATE TABLE IF NOT EXISTS payment_link_settings (
                 id                 INT PRIMARY KEY DEFAULT 1,
                 app_domain         VARCHAR(255) NULL,
                 play_store_url     VARCHAR(500) NULL,
                 sha256_fingerprint VARCHAR(255) NULL,
                 fee_amount         INT NOT NULL DEFAULT 0,
                 fee_threshold      INT NOT NULL DEFAULT 5,
                 active             TINYINT(1) NOT NULL DEFAULT 0,
                 updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                             ON UPDATE CURRENT_TIMESTAMP
               ) ENGINE=InnoDB"""
        )
        try:
            cur.execute(
                "ALTER TABLE transactions ADD COLUMN payment_link_code VARCHAR(16) NULL, "
                "ADD INDEX idx_link_code (payment_link_code)"
            )
        except mysql.connector.Error as exc:
            # 1060 = Duplicate column name — already applied on an earlier
            # startup; anything else is a real problem worth surfacing.
            if exc.errno != 1060:
                raise
        try:
            # Ties a Request-to-Pay back to the link it was billing, so the
            # moment `get_fee_collection_status` sees it turn successful it
            # knows which link's `fee_charges_done` to credit — the owner's
            # own app started this charge (never the server on its own), so
            # this is only ever set when charge-fee was called from there.
            cur.execute(
                "ALTER TABLE fee_collections ADD COLUMN payment_link_code VARCHAR(16) NULL, "
                "ADD INDEX idx_fee_link_code (payment_link_code)"
            )
        except mysql.connector.Error as exc:
            if exc.errno != 1060:
                raise

import os
import sys
import sqlite3
import logging
import threading
from contextlib import contextmanager

import mysql.connector
from mysql.connector import pooling

from config import Config

logger = logging.getLogger(__name__)

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DB_FILE = os.path.join(BASE_DIR, "accounts.db")
_db_mode = None
_mysql_pool = None
_pool_lock = threading.Lock()


class SQLiteWrapper:
    def __init__(self, conn):
        self.conn = conn

    def cursor(self, dictionary=True):
        cur = self.conn.cursor()
        return SQLiteCursorWrapper(cur)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


class SQLiteCursorWrapper:
    def __init__(self, cursor):
        self.cursor = cursor
        self.lastrowid = None

    def execute(self, query, params=None):
        q = query.replace("%s", "?")
        q = q.replace("INT AUTO_INCREMENT PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
        q = q.replace("TIMESTAMP DEFAULT CURRENT_TIMESTAMP", "DATETIME DEFAULT CURRENT_TIMESTAMP")

        if params is None:
            res = self.cursor.execute(q)
        else:
            res = self.cursor.execute(q, params)
        self.lastrowid = self.cursor.lastrowid
        return res

    def fetchone(self):
        row = self.cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    def fetchall(self):
        rows = self.cursor.fetchall()
        if not rows:
            return []
        return [dict(r) for r in rows]

    def close(self):
        self.cursor.close()


def get_db_mode():
    global _db_mode
    if _db_mode is None:
        try:
            conn = mysql.connector.connect(
                host=Config.MYSQL_HOST,
                port=Config.MYSQL_PORT,
                user=Config.MYSQL_USER,
                password=Config.MYSQL_PASSWORD,
                connect_timeout=2,
            )
            conn.close()
            _db_mode = "mysql"
            logger.info("Database mode: MySQL")
        except Exception:
            _db_mode = "sqlite"
            logger.info("Database mode: SQLite (fallback)")
    return _db_mode


def _get_mysql_pool():
    global _mysql_pool
    if _mysql_pool is None:
        with _pool_lock:
            if _mysql_pool is None:
                try:
                    init_conn = mysql.connector.connect(
                        host=Config.MYSQL_HOST,
                        port=Config.MYSQL_PORT,
                        user=Config.MYSQL_USER,
                        password=Config.MYSQL_PASSWORD,
                    )
                    init_cur = init_conn.cursor()
                    init_cur.execute(
                        f"CREATE DATABASE IF NOT EXISTS `{Config.MYSQL_DB}`"
                    )
                    init_cur.close()
                    init_conn.close()

                    _mysql_pool = pooling.MySQLConnectionPool(
                        pool_name="accounts_pool",
                        pool_size=5,
                        pool_reset_session=True,
                        host=Config.MYSQL_HOST,
                        port=Config.MYSQL_PORT,
                        user=Config.MYSQL_USER,
                        password=Config.MYSQL_PASSWORD,
                        database=Config.MYSQL_DB,
                    )
                    logger.info("MySQL connection pool created (size=5)")
                except Exception as e:
                    logger.error(f"Failed to create MySQL pool: {e}")
                    _mysql_pool = None
    return _mysql_pool


def get_db_connection():
    mode = get_db_mode()
    if mode == "mysql":
        try:
            pool = _get_mysql_pool()
            if pool:
                return pool.get_connection()
            conn = mysql.connector.connect(
                host=Config.MYSQL_HOST,
                port=Config.MYSQL_PORT,
                user=Config.MYSQL_USER,
                password=Config.MYSQL_PASSWORD,
                database=Config.MYSQL_DB,
            )
            return conn
        except Exception as e:
            logger.error(f"MySQL connection failed ({e}), falling back to SQLite")

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return SQLiteWrapper(conn)


@contextmanager
def get_db_cursor(dictionary=True):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=dictionary)
    try:
        yield cursor, conn
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cursor.close()
        conn.close()


def get_jai_agency_db_path():
    """Resolve active database path for Jai Agency (SQLite)."""
    db_path = getattr(Config, "JAI_AGENCY_DB_PATH", r"C:\ProgramData\jai agency\JAI_AGENCY.db")
    if not os.path.exists(db_path):
        backup_path = getattr(Config, "JAI_AGENCY_BACKUP_PATH", r"D:\projects\Jai Agency\datebase\JAI_AGENCY.db")
        if os.path.exists(backup_path):
            db_path = backup_path
        else:
            proj_db = os.path.join(getattr(Config, "JAI_AGENCY_PROJECT_DIR", r"D:\projects\Jai Agency"), "inventory.db")
            if os.path.exists(proj_db):
                db_path = proj_db
            else:
                logger.warning(f"Jai Agency DB not found at {db_path} or {backup_path}")
                return None
    return db_path


def get_jai_agency_db_connection():
    """Connect to Jai Agency SQLite database (contains both Sales and Inventory)."""
    db_path = get_jai_agency_db_path()
    if not db_path:
        return None
    try:
        conn = sqlite3.connect(db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn
    except Exception as e:
        logger.error(f"Failed to connect to Jai Agency DB ({db_path}): {e}")
        return None


def get_sales_db_connection():
    """Connect to Sales database (Jai Agency SQLite)."""
    return get_jai_agency_db_connection()


def get_inventory_db_connection():
    """Connect to Inventory database (Jai Agency SQLite)."""
    return get_jai_agency_db_connection()


def generate_unique_number(prefix, table_name, column_name, length=13):
    import uuid
    import time

    for _ in range(10):
        uid = uuid.uuid4().int % (10 ** length)
        number = f"{prefix}-{uid}"
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                f"SELECT COUNT(*) AS cnt FROM {table_name} WHERE {column_name} = %s;",
                (number,),
            )
            row = cursor.fetchone()
            cnt = row["cnt"] if isinstance(row, dict) else list(row.values())[0]
            if cnt == 0:
                return number
        finally:
            cursor.close()
            conn.close()
        time.sleep(0.001)

    ts = int(time.time() * 1000) % (10 ** length)
    return f"{prefix}-{ts}"

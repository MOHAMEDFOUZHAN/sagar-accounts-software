import os
import sqlite3
import mysql.connector
from mysql.connector import pooling
from config import Config

DB_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "accounts.db")
_db_mode = None  # 'mysql' or 'sqlite'

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
        # Translate MySQL query placeholders %s -> ?
        q = query.replace('%s', '?').replace('INT AUTO_INCREMENT PRIMARY KEY', 'INTEGER PRIMARY KEY AUTOINCREMENT')
        q = q.replace('TIMESTAMP DEFAULT CURRENT_TIMESTAMP', 'DATETIME DEFAULT CURRENT_TIMESTAMP')
        q = q.replace('DATETIME DEFAULT CURRENT_TIMESTAMP', 'DATETIME DEFAULT CURRENT_TIMESTAMP')
        
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
        if isinstance(row, sqlite3.Row):
            return dict(row)
        return row

    def fetchall(self):
        rows = self.cursor.fetchall()
        if not rows:
            return []
        if isinstance(rows[0], sqlite3.Row):
            return [dict(r) for r in rows]
        return rows

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
                connect_timeout=2
            )
            conn.close()
            _db_mode = 'mysql'
        except Exception:
            _db_mode = 'sqlite'
    return _db_mode

def get_db_connection():
    mode = get_db_mode()
    if mode == 'mysql':
        try:
            # First ensure database exists
            init_conn = mysql.connector.connect(
                host=Config.MYSQL_HOST,
                port=Config.MYSQL_PORT,
                user=Config.MYSQL_USER,
                password=Config.MYSQL_PASSWORD
            )
            init_cur = init_conn.cursor()
            init_cur.execute(f"CREATE DATABASE IF NOT EXISTS `{Config.MYSQL_DB}`")
            init_cur.close()
            init_conn.close()

            return mysql.connector.connect(
                host=Config.MYSQL_HOST,
                port=Config.MYSQL_PORT,
                user=Config.MYSQL_USER,
                password=Config.MYSQL_PASSWORD,
                database=Config.MYSQL_DB
            )
        except Exception as e:
            print(f"[!] MySQL connection failed ({e}), falling back to SQLite.")
            
    # SQLite Fallback
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return SQLiteWrapper(conn)

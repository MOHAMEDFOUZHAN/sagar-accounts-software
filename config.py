import os
import sys
import secrets
from dotenv import load_dotenv

if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

env_path = os.path.join(APP_DIR, ".env")
if os.path.exists(env_path):
    load_dotenv(env_path)
else:
    load_dotenv()


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)

    MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
    MYSQL_PORT = int(os.getenv("MYSQL_PORT", 3306))
    MYSQL_USER = os.getenv("MYSQL_USER", "root")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DB = os.getenv("MYSQL_DB", "accounts_db")

    PORT = int(os.getenv("FLASK_PORT", 5001))
    DEBUG = os.getenv("FLASK_DEBUG", "false").lower() == "true"

    # External Integrated System: Jai Agency (Unified Sales & Inventory)
    JAI_AGENCY_DB_PATH = os.getenv("JAI_AGENCY_DB_PATH", r"C:\ProgramData\jai agency\JAI_AGENCY.db")
    JAI_AGENCY_BACKUP_PATH = os.getenv("JAI_AGENCY_BACKUP_PATH", r"D:\projects\Jai Agency\datebase\JAI_AGENCY.db")
    JAI_AGENCY_PROJECT_DIR = os.getenv("JAI_AGENCY_PROJECT_DIR", r"D:\projects\Jai Agency")

    # Legacy Aliases for backward compatibility
    SALES_DB = os.getenv("SALES_DB", "maple_pro_db")
    INVENTORY_DB_PATH = JAI_AGENCY_DB_PATH
    INVENTORY_BACKUP_PATH = JAI_AGENCY_BACKUP_PATH

    ITEMS_PER_PAGE = 25
    LOGIN_RATE_LIMIT = 30
    LOGIN_RATE_WINDOW = 300

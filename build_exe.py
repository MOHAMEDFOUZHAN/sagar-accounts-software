import os
import sys
import shutil
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def build():
    print("=" * 70)
    print("      BUILDING STANDALONE SAGAR ACCOUNTS EXECUTABLE")
    print("=" * 70)

    # 1. Paths
    app_entry = os.path.join(BASE_DIR, "app.py")
    frontend_dir = os.path.join(BASE_DIR, "frontend")
    dist_dir = os.path.join(BASE_DIR, "dist")
    build_dir = os.path.join(BASE_DIR, "build")

    # 2. PyInstaller command arguments
    # We include frontend, backend, database, and config
    sep = ";" if sys.platform == "win32" else ":"

    cmd = [
        sys.executable,
        "-m", "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--name", "JaiAgencyAccounts",
        f"--add-data={frontend_dir}{sep}frontend",
        f"--add-data={os.path.join(BASE_DIR, 'config.py')}{sep}.",
        f"--add-data={os.path.join(BASE_DIR, 'init_db.py')}{sep}.",
        "--hidden-import=mysql.connector",
        "--hidden-import=mysql.connector.pooling",
        "--hidden-import=sqlite3",
        "--hidden-import=reportlab",
        "--hidden-import=reportlab.platypus",
        "--hidden-import=reportlab.lib",
        "--hidden-import=reportlab.pdfgen",
        "--hidden-import=backend",
        "--hidden-import=backend.auth",
        "--hidden-import=backend.db",
        "--hidden-import=backend.audit_engine",
        "--hidden-import=backend.backup_engine",
        "--hidden-import=backend.db_integrity",
        "--hidden-import=backend.sync_engine",
        "--hidden-import=backend.accounts_engine",
        "--hidden-import=backend.reports_engine",
        "--hidden-import=backend.coa_engine",
        "--hidden-import=backend.double_entry_engine",
        "--hidden-import=backend.ledger_engine",
        "--hidden-import=backend.accounting_rules",
        "--hidden-import=backend.period_engine",
        "--hidden-import=backend.opening_balance_engine",
        "--hidden-import=backend.reconciliation_engine",
        "--hidden-import=backend.health_check",
        "--hidden-import=backend.validation_engine",
        "--hidden-import=backend.business_accounting",
        "--hidden-import=init_db",
        "--hidden-import=config",
        app_entry
    ]

    print("[*] Running PyInstaller command:")
    print(" ".join(cmd))
    print("-" * 70)

    result = subprocess.run(cmd, cwd=BASE_DIR)

    if result.returncode != 0:
        print("[!] PyInstaller build failed with exit code:", result.returncode)
        sys.exit(result.returncode)

    # 3. Post-build tasks
    out_dir = os.path.join(dist_dir, "JaiAgencyAccounts")
    print("\n[+] Build successful! Output directory:", out_dir)

    # Copy .env if exists
    env_file = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_file):
        shutil.copy2(env_file, os.path.join(out_dir, ".env"))
        print("[+] Copied .env configuration to dist folder.")

    # Write a quick instructions file in the dist folder
    instructions_path = os.path.join(out_dir, "HOW_TO_RUN.txt")
    with open(instructions_path, "w", encoding="utf-8") as f:
        f.write(
            "JAI AGENCY ACCOUNTS SOFTWARE - STANDALONE RELEASE\n"
            "=================================================\n\n"
            "1. HOW TO RUN:\n"
            "   - Double-click 'JaiAgencyAccounts.exe'.\n"
            "   - A console window will appear and your default web browser will automatically open:\n"
            "     http://127.0.0.1:5050\n\n"
            "2. DEFAULT LOGIN CREDENTIALS:\n"
            "   - Username: accounts\n"
            "   - Password: 1234\n\n"
            "   (Alternative Admin Role)\n"
            "   - Username: accountant\n"
            "   - Password: 1234\n\n"
            "3. INTEGRATION WITH JAI AGENCY:\n"
            "   - Reads sales bills and inventory batches from C:\\ProgramData\\jai agency\\JAI_AGENCY.db\n"
            "   - The sync is 100% read-only and will never alter or harm Jai Agency data.\n\n"
            "4. BACKUPS & DATA:\n"
            "   - All accounts databases and backups are saved right here in this folder.\n"
        )
    print("[+] Created HOW_TO_RUN.txt in dist folder.")
    print("=" * 70)
    print("BUILD COMPLETE! You can find the executable at:")
    print(os.path.join(out_dir, "JaiAgencyAccounts.exe"))
    print("=" * 70)

if __name__ == '__main__':
    build()

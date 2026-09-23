# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:/projects/accounts software/app.py'],
    pathex=[],
    binaries=[],
    datas=[('D:/projects/accounts software/frontend', 'frontend'), ('D:/projects/accounts software/config.py', '.'), ('D:/projects/accounts software/init_db.py', '.')],
    hiddenimports=['mysql.connector', 'mysql.connector.pooling', 'sqlite3', 'reportlab', 'reportlab.platypus', 'reportlab.lib', 'reportlab.pdfgen', 'backend', 'backend.auth', 'backend.db', 'backend.audit_engine', 'backend.backup_engine', 'backend.db_integrity', 'backend.sync_engine', 'backend.accounts_engine', 'backend.reports_engine', 'backend.coa_engine', 'backend.double_entry_engine', 'backend.ledger_engine', 'backend.accounting_rules', 'backend.period_engine', 'backend.opening_balance_engine', 'backend.reconciliation_engine', 'backend.health_check', 'backend.validation_engine', 'backend.business_accounting', 'init_db', 'config'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SagarAccounts',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SagarAccounts',
)

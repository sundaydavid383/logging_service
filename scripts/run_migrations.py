#!/usr/bin/env python3
"""
scripts/run_migrations.py
--------------------------
Run all pending Alembic migrations and print the result.

Usage:
    python scripts/run_migrations.py           # upgrade to head
    python scripts/run_migrations.py --check   # check without applying
    python scripts/run_migrations.py --history # show migration history
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Force resolve root directory to prevent path scoping discrepancies across containers
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from alembic import command
from alembic.config import Config
from fdq_commons.config import settings


def get_alembic_config() -> Config:
    # Anchor the ini resolution to the root path natively
    ini_path = ROOT_DIR / "alembic.ini"
    if not ini_path.exists():
        print(f"[FDQ Migrations] [ERROR] Could not locate configuration at: {ini_path}")
        sys.exit(1)
        
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", settings.postgres_dsn)
    return cfg


def run_upgrade() -> None:
    print(f"[FDQ Migrations] Connecting to: {settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}")
    try:
        cfg = get_alembic_config()
        command.upgrade(cfg, "head")
        print("[FDQ Migrations] All migrations applied successfully.")
    except Exception as exc:
        print(f"[FDQ Migrations] [FATAL] Upgrade execution failed: {exc}")
        sys.exit(1)


def run_check() -> None:
    from sqlalchemy import create_engine
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    print("[FDQ Migrations] Checking schema synchronization...")
    engine = create_engine(settings.postgres_dsn)
    
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(conn)
            current = context.get_current_revision()

        cfg = get_alembic_config()
        sd = ScriptDirectory.from_config(cfg)
        head = sd.get_current_head()

        if current == head:
            print(f"[FDQ Migrations] Up to date. Revision: {current}")
            sys.exit(0)
        else:
            print(f"[FDQ Migrations] PENDING CHANGES DETECTED. Current: {current} → Head: {head}")
            sys.exit(1)
            
    except Exception as exc:
        print(f"[FDQ Migrations] [ERROR] Pre-flight check failed: {exc}")
        sys.exit(1)
    finally:
        # Prevent engine sockets from dangling open across orchestration platforms
        engine.dispose()


def run_history() -> None:
    try:
        cfg = get_alembic_config()
        command.history(cfg, verbose=True)
    except Exception as exc:
        print(f"[FDQ Migrations] [ERROR] Could not fetch history: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FDQ Migration Runner")
    parser.add_argument("--check",   action="store_true", help="Assert schema status without writing changes")
    parser.add_argument("--history", action="store_true", help="Display full Alembic historical version log")
    args = parser.parse_args()

    if args.check:
        run_check()
    elif args.history:
        run_history()
    else:
        run_upgrade()
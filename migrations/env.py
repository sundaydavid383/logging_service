"""
migrations/env.py
------------------
Alembic environment configuration tailored for native psycopg2 / Dataclass models.

The database URL is read from fdq_commons.config.settings so it is
always in sync with the rest of the application. Never hardcode a DSN here.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import your dynamic settings
from fdq_commons.config import settings

# This is the Alembic Config object
config = context.config

# Overwriting the placeholder url from alembic.ini with our real configuration DSN string
config.set_main_option("sqlalchemy.url", settings.postgres_dsn)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set to None because your tables are managed via raw SQL fragments and dataclasses
target_metadata = None


# Only manage tables that belong to FDQ — ignore everything else
FDQ_TABLES = {
    "activity_logs",
    "error_logs", 
    "audit_events",
    "notification_logs",
    "notification_templates",
    "notification_preferences",
    "event_type_registry",
}

def include_object(object_, name, type_, reflected, compare_to) -> bool:
    if type_ == "table" and reflected and name not in FDQ_TABLES:
        # This table exists in the DB but is not ours — ignore it
        return False
    return True


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # Prevents connection socket leakages
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
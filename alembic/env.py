from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from src.core.config import get_settings

# Models are wired up in step 4. Until then, target_metadata stays None and
# autogenerate is a no-op.
try:
    from shared.models import Base  # type: ignore[attr-defined]

    target_metadata = Base.metadata
except (ImportError, AttributeError):
    target_metadata = None

config = context.config

if config.config_file_name is not None:
    # Never disable the app's already-created loggers: fileConfig's
    # default would silence every `src.*` logger created before a
    # migration runs (in-process runs: tests, start.sh boot upgrade).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url_sync)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

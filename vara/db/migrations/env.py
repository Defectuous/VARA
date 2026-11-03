from alembic import context
from sqlalchemy import create_engine

from vara.db.models import Base

config = context.config


def _include_name(name, type_, parent_names) -> bool:
    # FTS5 virtual tables and their shadow tables are managed by hand in migrations.
    return not (type_ == "table" and name and "_fts" in name)


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        render_as_batch=True,
        include_name=_include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:
    with create_engine(config.get_main_option("sqlalchemy.url")).connect() as connection:
        _run(connection)

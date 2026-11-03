from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

MIGRATIONS = Path(__file__).parent / "migrations"


def _alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", url)
    return config


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.url = f"sqlite:///{path.as_posix()}"
        # FastAPI runs sync work in a thread pool, so connections cross threads.
        self.engine: Engine = create_engine(self.url, connect_args={"check_same_thread": False})
        event.listen(self.engine, "connect", _set_pragmas)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def upgrade(self) -> None:
        """Apply any pending schema migrations."""
        config = _alembic_config(self.url)
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self._sessions() as session, session.begin():
            yield session

    def dispose(self) -> None:
        self.engine.dispose()


def _set_pragmas(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    # Overwrite deleted content on disk, so deleting a memory or conversation really removes it.
    cursor.execute("PRAGMA secure_delete = ON")
    cursor.close()

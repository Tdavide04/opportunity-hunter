"""SQLAlchemy store: owns a standalone engine or wraps one supplied by PCH."""
from sqlalchemy import Engine, create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from opportunity_hunter.db import Base


class SqlAlchemyStore:
    def __init__(
        self,
        engine: Engine | None = None,
        session_factory: sessionmaker[Session] | None = None,
        database_url: str | None = None,
    ) -> None:
        supplied = sum(value is not None for value in (engine, session_factory, database_url))
        if supplied != 1:
            raise ValueError("provide exactly one of engine, session_factory or database_url")
        self._owns_engine = database_url is not None
        if database_url is not None:
            engine = create_engine(database_url, pool_pre_ping=True, connect_args={"connect_timeout": 3})
        if session_factory is not None:
            self.session_factory = session_factory
            bound = session_factory.kw.get("bind")
            if not isinstance(bound, Engine):
                raise ValueError("session_factory must be bound to an SQLAlchemy Engine")
            self.engine = bound
        else:
            assert engine is not None
            self.engine = engine
            self.session_factory = sessionmaker(engine, expire_on_commit=False)

    def initialize_fresh_database(self) -> None:
        """Create tables only in a database with no user tables; never use on an existing/embedded database."""
        existing = set(inspect(self.engine).get_table_names())
        if existing:
            raise RuntimeError("refusing to initialize a non-empty database; use the separately reviewed Alembic procedure")
        Base.metadata.create_all(self.engine)

    def close(self) -> None:
        if self._owns_engine:
            self.engine.dispose()

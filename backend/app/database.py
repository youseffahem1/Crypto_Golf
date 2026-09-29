from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import DATABASE_URL

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
    # A Render/Heroku database is a managed instance sitting behind a proxy that
    # will drop an idle TCP connection without warning. pool_pre_ping (above)
    # catches a stale connection before it is handed out, but without recycle a
    # connection can still be sitting idle long enough for the server to have
    # closed it, and the *next* statement is what discovers it -- as a failed
    # request, in the middle of a user-facing call. Recycling well inside the
    # proxy's idle timeout replaces that with a connection that is simply
    # replaced in the background. This does not raise pool_size or
    # max_overflow; it changes nothing about how many connections the app may
    # hold, only how old a connection is allowed to get.
    pool_recycle=1800,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope(factory=None):
    """A session that is ALWAYS returned to the pool.

    `get_db` is for request-scoped FastAPI dependencies. Background loops are
    not request-scoped: they run on a schedule, with no framework guaranteeing
    a teardown, and they must survive an exception, an early return and
    cancellation. A `try/finally` in a loop body is easy to get wrong once
    there are several exit points, and the failure mode is silent and
    catastrophic — every un-closed session pins one pooled connection
    permanently, so the pool drains and *login* starts timing out waiting for a
    connection that some background sweep is still holding.

    Using a context manager makes the release structural instead of
    remembered: leaving the block by any route at all, including an exception
    propagating out of the body, closes the session and returns the connection.
    Nothing here is global and nothing is long-lived; a new session is created
    per block and closed at its end.
    """
    db = (factory or SessionLocal)()
    try:
        yield db
    finally:
        db.close()

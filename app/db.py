from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base


def make_engine(
    url: str,
    *,
    # Per-engine sizing: replica needs more connections than primary or sync.
    # Defaults match the old hardcoded values so callers that omit these are unaffected.
    pool_size: int = 10,
    max_overflow: int = 0,
):
    args = {"check_same_thread": False} if "sqlite" in url else {}
    return create_engine(
        url,
        connect_args=args,
        pool_timeout=5,
        pool_recycle=3600,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
    )


Base = declarative_base()

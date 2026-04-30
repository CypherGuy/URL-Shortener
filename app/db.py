from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base


def make_engine(url: str):
    args = {"check_same_thread": False} if "sqlite" in url else {}
    return create_engine(
        url,
        connect_args=args,
        pool_timeout=5,
        pool_recycle=3600,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=0,
    )


Base = declarative_base()

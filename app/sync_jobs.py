from contextlib import asynccontextmanager
import threading
import time
from typing import NoReturn

from redis import exceptions
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import Code


def every(seconds: float, func, *args, **kwargs) -> NoReturn:
    while True:
        func(*args, **kwargs)
        time.sleep(seconds)


def sync_to_db(cache, sync_engine) -> None:
    with Session(sync_engine) as session:
        try:
            keys = cache.keys("clicks:*")
            for key in keys:
                short_code = key.removeprefix("clicks:")
                url = session.query(Code).filter_by(short_code_chars=short_code).one_or_none()
                if url:
                    clicks = cache.get(key)
                    url.clicks = clicks
            session.commit()

        except exceptions.RedisError:
            print("Redis is down")
        except IntegrityError:
            session.rollback()
        except SQLAlchemyError as error:
            print(f"SQLAlchemy Error: {error}")


def sync_to_replica(sync_engine, sync_replica_engine) -> None:
    with Session(sync_engine) as session:
        urls = session.query(Code).all()
        session.expunge_all()

    primary_codes = {url.short_code_chars for url in urls}

    with Session(sync_replica_engine) as replica_session:
        try:
            replica_codes = {
                code
                for (code,) in replica_session.query(Code.short_code_chars).all()
            }

            stale = replica_codes - primary_codes
            if stale:
                replica_session.query(Code).filter(Code.short_code_chars.in_(stale)).delete()

            for url in urls:
                replica_session.merge(url)

            replica_session.commit()

        except IntegrityError:
            replica_session.rollback()
        except SQLAlchemyError as error:
            print(f"SQLAlchemy Error: {error}")


@asynccontextmanager
async def lifespan(app, sync_to_db_func, sync_to_replica_func):
    thread_primary = threading.Thread(target=every, args=(30, sync_to_db_func), daemon=True)
    thread_replica = threading.Thread(target=every, args=(60, sync_to_replica_func), daemon=True)
    thread_primary.start()
    thread_replica.start()
    yield

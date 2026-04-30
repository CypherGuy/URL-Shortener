from contextlib import asynccontextmanager
import threading
import time
from typing import NoReturn

from redis import exceptions
from sqlalchemy import select
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
            if not keys:
                return

            # Build {short_code: click_count} from Redis in one go.
            click_map = {
                key.removeprefix("clicks:"): cache.get_int(key) for key in keys
            }

            rows = session.execute(
                select(Code).where(Code.short_code_chars.in_(list(click_map.keys())))
            ).scalars().all()

            for row in rows:
                row.clicks = click_map[row.short_code_chars]

            # Commits eerything in one UPDATE, reducing the number of db calls
            session.commit()

        except exceptions.RedisError:
            print("Redis is down")
        except IntegrityError:
            session.rollback()
        except SQLAlchemyError as error:
            print(f"SQLAlchemy Error: {error}")


@asynccontextmanager
async def lifespan(app, sync_to_db_func):
    thread_primary = threading.Thread(target=every, args=(30, sync_to_db_func), daemon=True)
    thread_primary.start()
    yield

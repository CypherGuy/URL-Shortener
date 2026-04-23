from contextlib import asynccontextmanager
import threading
import time
from typing import Annotated, NoReturn

import redis
from redis import RedisError, exceptions
from app.cache import RedisCache
from fastapi import Depends, FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import RedirectResponse
from sqlalchemy import create_engine, Column, String, DateTime, Integer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
import os
from dotenv import load_dotenv
import string
import random
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./test.db")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

if "sqlite" in DATABASE_URL:
    engine = create_engine(DATABASE_URL, connect_args={
        "check_same_thread": False}, pool_timeout=5, pool_recycle=3600, pool_pre_ping=True)
else:
    engine = create_engine(DATABASE_URL, connect_args={}, pool_timeout=5, pool_recycle=3600, pool_pre_ping=True)

if "sqlite" in DATABASE_URL:
    sync_engine = create_engine(DATABASE_URL, connect_args={
        "check_same_thread": False}, pool_timeout=5, pool_recycle=3600, pool_pre_ping=True)
else:
    sync_engine = create_engine(DATABASE_URL, connect_args={}, pool_timeout=5, pool_recycle=3600, pool_pre_ping=True)
Base = declarative_base()


r = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=6379, db=0, decode_responses=True)
r = RedisCache(redis_client=r)

# === Models ===


class Code(Base):
    __tablename__ = "codes"

    id = Column(Integer, primary_key=True)
    clicks = Column(Integer, default=0, nullable=False)
    short_code_chars = Column(String, nullable=False,
                              unique=True)  # 123456ABC0
    original_url = Column(String(1500), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(
        # Lambda ensures the timestamp is set at insert time, not when the module loads
        timezone.utc), nullable=False)


class URLRequest(BaseModel):
    original_url: str = Field(min_length=10, max_length=1500)


class URLResponse(BaseModel):

    # https://www.localhost:8000/123456ABC0
    short_url: str
    created_at: datetime
    original_url: str


class StatsResponse(BaseModel):
    clicks: int
    created_at: datetime
    original_url: str


Base.metadata.create_all(bind=engine)

# === Session and context management


def get_session():
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def increment_click(short_code: str) -> None:
    r.increment(f"clicks:{short_code}")


def every(seconds: float, func, *args, **kwargs) -> NoReturn:
    while True:
        func(*args, **kwargs)
        time.sleep(seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    t = threading.Thread(target=every, args=(30, sync_to_db), daemon=True)
    t.start()
    yield


def sync_to_db() -> None:
    with Session(sync_engine) as session:
        try:
            keys = r.keys("clicks:*")
            for key in keys:
                short_code = key.removeprefix("clicks:")
                url = session.query(Code).filter_by(
                    short_code_chars=short_code).one_or_none()
                if url:
                    clicks = r.get(key)
                    url.clicks = clicks
            session.commit()

        except exceptions.RedisError:
            print("Redis is down")
        except IntegrityError:
            session.rollback()
        except SQLAlchemyError as e:
            print(f"SQLAlchemy Error: {e}")


app = FastAPI(lifespan=lifespan)


# === Endpoints ===


@app.get("/health")
def root():

    return {"status": "ok"}


@app.get("/{short_code}")
def get_code(short_code: str, session: SessionDep, background_tasks: BackgroundTasks) -> RedirectResponse:
    cached_url = r.get(short_code)
    if cached_url:
        background_tasks.add_task(increment_click, short_code)
        return RedirectResponse(cached_url, status_code=302)
    else:
        # Not in cache, try db
        url = session.query(Code).filter_by(
            short_code_chars=short_code).one_or_none()
        if not url:
            raise HTTPException(status_code=404, detail="URL not found")
        else:
            r.set(short_code, url.original_url)
            r.set(f"clicks:{short_code}", url.clicks)
            background_tasks.add_task(increment_click, short_code)
            return RedirectResponse(url.original_url, status_code=302)


@app.delete("/{short_code}", status_code=204)
def delete_code(short_code: str, session: SessionDep) -> None:

    cached_url = r.get(short_code)
    if cached_url:
        r.delete(short_code)

    url = session.query(Code).filter_by(
        short_code_chars=short_code).one_or_none()
    if not url:
        raise HTTPException(status_code=404, detail="URL not found")
    else:
        session.delete(url)
        session.commit()


@app.get("/stats/{short_code}")
def get_stats(short_code: str, session: SessionDep) -> StatsResponse:
    # For this function, as clicks gets updated very frequently and the original url and created_at are static,
    # we call clicks from the cache and the other two from the db. This works because get_stats isn't called
    # nearly as much as get_code.

    try:
        original_url = None
        clicks = r.get_int(f"clicks:{short_code}")

        cached_url = r.get(short_code)
        if cached_url:
            original_url = str(cached_url)
    except RedisError:
        clicks = None
        original_url = None
    db_row = session.query(Code).filter_by(
        short_code_chars=short_code).one_or_none()
    if not db_row:
        raise HTTPException(status_code=404, detail="URL not found")
    else:
        if not original_url:
            original_url = db_row.original_url
        if not clicks:
            clicks = db_row.clicks

    created_at = db_row.created_at
    return StatsResponse(clicks=clicks, created_at=created_at, original_url=original_url)


@app.post("/shorten", status_code=201)
def shorten(url_request: URLRequest, session: SessionDep) -> URLResponse:

    # If the URL doesn't start with http:// or https://, add it
    original_url = url_request.original_url
    if not original_url.startswith(("http://", "https://")):
        original_url = "https://" + original_url

    # Retry up to 10 times in case of a short code collision
    for _ in range(10):
        try:
            chars = string.ascii_letters + string.digits
            short_code_chars = "".join(random.choices(chars, k=10))

            model = Code(short_code_chars=short_code_chars,
                         original_url=original_url)

            session.add(model)
            session.commit()
            session.refresh(model)

            response = URLResponse(

                short_url=f"{BASE_URL}/{short_code_chars}",
                created_at=model.created_at,
                original_url=original_url
            )

            return response

        except IntegrityError:
            session.rollback()
            continue

    raise HTTPException(
        status_code=500,
        detail="Failed to generate unique short code after 10 attempts. Please try again."
    )

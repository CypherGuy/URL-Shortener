from contextlib import asynccontextmanager
from typing import Annotated

import redis
from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session

from app.cache import RedisCache
from app.config import BASE_URL, DATABASE_URL, READ_REPLICA_URL, REDIS_HOST, REDIS_PORT
from app.db import Base, make_engine
from app.routes.health import router as health_router
from app.routes.urls import create_urls_router
from app.sync_jobs import lifespan as sync_lifespan
from app.sync_jobs import sync_to_db as sync_to_db_job

# Incoming traffic is approximately 80% read 20% write.
# web_replica_engine handles redirects + stats and web_engine handles writes only
# sync_engine runs one connection every 30s
web_engine = make_engine(DATABASE_URL, pool_size=25, max_overflow=5)
web_replica_engine = make_engine(READ_REPLICA_URL, pool_size=25, max_overflow=5)
sync_engine = make_engine(DATABASE_URL, pool_size=2, max_overflow=0)

redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
r = RedisCache(redis_client=redis_client)

Base.metadata.create_all(bind=web_engine)


def get_session():
    with Session(web_engine) as session:
        yield session


def get_replica_session():
    with Session(web_replica_engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
ReplicaSessionDep = Annotated[Session, Depends(get_replica_session)]


def get_cache() -> RedisCache:
    return r


def increment_click(short_code: str) -> None:
    get_cache().increment(f"clicks:{short_code}")


def sync_to_db() -> None:
    sync_to_db_job(get_cache(), sync_engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with sync_lifespan(app, sync_to_db):
        yield


app = FastAPI(lifespan=lifespan)
app.include_router(health_router)
app.include_router(
    create_urls_router(
        get_session=get_session,
        get_replica_session=get_replica_session,
        get_cache=get_cache,
        increment_click=increment_click,
        base_url=BASE_URL,
    )
)

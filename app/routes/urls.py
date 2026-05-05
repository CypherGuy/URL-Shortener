import random
import string
from datetime import datetime
from typing import Callable, cast

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import RedirectResponse
from redis import RedisError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Code
from app.schemas import StatsResponse, URLRequest, URLResponse


def create_urls_router(
    *,
    get_session: Callable,
    get_replica_session: Callable,
    get_cache: Callable,
    increment_click: Callable,
    base_url: str,
) -> APIRouter:
    router = APIRouter()

    @router.get("/{short_code}")
    def get_code(
        short_code: str,
        background_tasks: BackgroundTasks,
        session: Session = Depends(get_replica_session),
    ) -> RedirectResponse:
        cache = get_cache()
        cached_url = cache.get(short_code)
        if cached_url:
            background_tasks.add_task(increment_click, short_code)
            return RedirectResponse(cached_url, status_code=302)

        url = session.query(Code).filter_by(short_code_chars=short_code).one_or_none()
        if not url:
            raise HTTPException(status_code=404, detail="URL not found")

        cache.set(short_code, url.original_url)
        cache.set(f"clicks:{short_code}", url.clicks)
        # Cache created_at so /stats can return early without hitting the DB.
        cache.set(f"created_at:{short_code}", url.created_at.isoformat())
        background_tasks.add_task(increment_click, short_code)
        return RedirectResponse(cast(str, url.original_url), status_code=302)

    @router.get("/stats/{short_code}")
    def get_stats(short_code: str, session: Session = Depends(get_replica_session)) -> StatsResponse:
        cache = get_cache()

        try:
            original_url = None
            clicks = cache.get_int(f"clicks:{short_code}")

            cached_url = cache.get(short_code)
            if cached_url:
                original_url = str(cached_url)

            cached_created_at = cache.get(f"created_at:{short_code}")
        except RedisError:
            clicks = None
            original_url = None
            cached_created_at = None

        # Full cache hit: skip the DB entirely, same as the redirect endpoint does.
        if original_url and clicks is not None and cached_created_at:
            return StatsResponse(
                clicks=clicks,
                created_at=datetime.fromisoformat(str(cached_created_at)),
                original_url=original_url,
            )

        db_row = session.query(Code).filter_by(short_code_chars=short_code).one_or_none()
        if not db_row:
            raise HTTPException(status_code=404, detail="URL not found")

        if not original_url:
            original_url = cast(str, db_row.original_url)
        if clicks is None:
            clicks = cast(int, db_row.clicks)

        return StatsResponse(
            clicks=cast(int, clicks),
            created_at=cast(datetime, db_row.created_at),
            original_url=cast(str, original_url),
        )

    @router.delete("/{short_code}", status_code=204)
    def delete_code(short_code: str, session: Session = Depends(get_session)) -> None:
        cache = get_cache()
        cache.delete(short_code)
        cache.delete(f"created_at:{short_code}")
        cache.delete(f"clicks:{short_code}")

        url = session.query(Code).filter_by(short_code_chars=short_code).one_or_none()

        # We don't use the cached url because if that's None, this key would stay in ElastiCache
        if url:
            cache.delete(url.original_url)

        if not url:
            raise HTTPException(status_code=404, detail="URL not found")

        session.delete(url)
        session.commit()

    @router.post("/shorten", status_code=201)
    def shorten(url_request: URLRequest, session: Session = Depends(get_session)) -> URLResponse:
        original_url = url_request.original_url
        if not original_url.startswith(("http://", "https://")):
            original_url = "https://" + original_url

        # We want to see if the url is in the cache
        cache = get_cache()
        short_code = cache.get(original_url)
        if short_code:
            created_at = cache.get(f"created_at:{short_code}")
            if created_at:
                return URLResponse(
                    short_url=f"{base_url}/{short_code}",
                    created_at=datetime.fromisoformat(str(created_at)),
                    original_url=original_url,
                )

            existing = session.query(Code).filter_by(short_code_chars=short_code).one_or_none()
            if existing:
                cache.set(f"created_at:{short_code}", existing.created_at.isoformat())
                return URLResponse(
                    short_url=f"{base_url}/{short_code}",
                    created_at=existing.created_at,
                    original_url=original_url,
                )

        for _ in range(10):
            try:
                chars = string.ascii_letters + string.digits
                short_code_chars = "".join(random.choices(chars, k=10))

                model = Code(short_code_chars=short_code_chars, original_url=original_url)
                session.add(model)
                session.commit()

                # Add to cache
                cache.set(short_code_chars, original_url)
                cache.set(original_url, short_code_chars, ttl=86400)  # ttl only needed here since the
                cache.set(f"clicks:{short_code_chars}", 0)
                cache.set(f"created_at:{short_code_chars}", model.created_at.isoformat())

                return URLResponse(
                    short_url=f"{base_url}/{short_code_chars}",
                    created_at=datetime.fromisoformat(model.created_at.isoformat()),
                    original_url=original_url,
                )
            except IntegrityError:
                session.rollback()
                continue

        raise HTTPException(
            status_code=500,
            detail="Failed to generate unique short code after 10 attempts. Please try again.",
        )

    return router

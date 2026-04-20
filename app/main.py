# For Section 1, we're using a Monolithic architecture. Basically everything in one file: DB, models, routes etc..


from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import create_engine, Column, String, DateTime, Integer
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, sessionmaker
from pydantic import BaseModel
import os
from dotenv import load_dotenv
import string
import random
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

# Load environment variables
load_dotenv()

# Database setup
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./test.db")
engine = create_engine(DATABASE_URL, connect_args={
                       "check_same_thread": False} if "sqlite" in DATABASE_URL else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# === Models ===


class Code(Base):  # DB Model

    __tablename__ = "codes"

    id = Column(Integer, primary_key=True)
    clicks = Column(Integer, default=0, nullable=False)
    short_code_chars = Column(String, nullable=False,
                              unique=True)  # 123456ABC0
    original_url = Column(String(1500), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(
        # Lambda ensures the timestamp is set at insert time, not when the module loads
        timezone.utc), nullable=False)


# Request Model using Pydantic as it's not stored in the db. We only include data that the user provides in the request.
class URLRequest(BaseModel):
    original_url: str


# Response Model also using Pydantic as it's not stored in the db.
class URLResponse(BaseModel):

    # https://www.shorturl.com/123456ABC0
    short_url: str
    created_at: datetime  # Just for testing
    # original_url added for confirmation on user side. What if they made a typo? Then they can see they did so
    original_url: str


# Creates all tables
Base.metadata.create_all(bind=engine)

# === Endpoints ===

# FastAPI app
app = FastAPI()


def get_session():
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


@app.get("/health")
async def root():
    return {"status": "ok"}


@app.get("/{short_code}")
async def get_code(short_code: str, session: SessionDep):
    url = session.query(Code).filter_by(
        short_code_chars=short_code).one_or_none()
    if not url:
        raise HTTPException(status_code=404, detail="URL not found")
    else:
        url.clicks += 1
        session.commit()
        return RedirectResponse(url.original_url, status_code=302)


@app.delete("/urls", status_code=204)
async def delete_all_codes(session: SessionDep):
    session.query(Code).delete()
    session.commit()


@app.delete("/{short_code}", status_code=204)
async def delete_code(short_code: str, session: SessionDep):
    url = session.query(Code).filter_by(
        short_code_chars=short_code).one_or_none()
    if not url:
        raise HTTPException(status_code=404, detail="URL not found")
    else:
        session.delete(url)
        session.commit()
        return {"short_code": short_code}


@app.get("/stats/{short_code}")
async def get_stats(short_code: str, session: SessionDep):
    url = session.query(Code).filter_by(
        short_code_chars=short_code).one_or_none()
    if not url:
        raise HTTPException(status_code=404, detail="URL not found")
    else:
        clicks = url.clicks
        created_at = url.created_at
        original_url = url.original_url

        return {"clicks": clicks, "created_at": created_at, "original_url": original_url}


@app.post("/shorten", status_code=201)
async def shorten(url_request: URLRequest, session: SessionDep):
    if url_request.original_url == "":
        raise HTTPException(
            status_code=400,
            detail="URL given is empty"
        )

    # If the URL doesn't start with http:// or https://, add it
    original_url = url_request.original_url
    if not original_url.startswith(("http://", "https://")):
        original_url = "https://" + original_url

    # Retry up to 10 times in case of a short code collision
    for count in range(10):
        try:
            # First step: Generate a list of 10 random alphanumeric chars
            chars = string.ascii_letters + string.digits
            short_code_chars = "".join(random.choices(chars, k=10))

            model = Code(short_code_chars=short_code_chars,
                         original_url=original_url)

            session.add(model)
            session.commit()
            session.refresh(model)

            response = URLResponse(

                short_url=f"http://localhost:8000/{short_code_chars}",
                created_at=model.created_at,
                original_url=original_url
            )

            return response

        except IntegrityError:
            # If we get here, the db is in a failed state. This reverts to an unfailed state
            session.rollback()
            continue

        except Exception as e:
            # If the original url is too long, a too_long error is returned
            if "too long" in str(e):
                raise HTTPException(status_code=400, detail="URL too long")

        if count == 9:
            raise HTTPException(
                status_code=500,
                detail="Failed to generate unique short code after 10 attempts. Please try again."
            )

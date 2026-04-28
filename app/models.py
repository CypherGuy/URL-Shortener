from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String

from app.db import Base


class Code(Base):
    __tablename__ = "codes"

    id = Column(Integer, primary_key=True)
    clicks = Column(Integer, default=0, nullable=False)
    short_code_chars = Column(String, nullable=False, unique=True)
    original_url = Column(String(1500), nullable=False)
    created_at = Column(
        DateTime,
        # Set timestamp at insert time, not module import time.
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

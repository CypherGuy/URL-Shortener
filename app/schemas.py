from datetime import datetime

from pydantic import BaseModel, Field


class URLRequest(BaseModel):
    original_url: str = Field(min_length=10, max_length=1500)


class URLResponse(BaseModel):
    short_url: str
    created_at: datetime
    original_url: str


class StatsResponse(BaseModel):
    clicks: int
    created_at: datetime
    original_url: str

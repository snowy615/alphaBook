from typing import Optional
import datetime as dt
from sqlmodel import SQLModel, Field

class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)

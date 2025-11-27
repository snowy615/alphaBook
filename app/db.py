# app/db.py
import os
from sqlmodel import SQLModel, create_engine, Session

# Railway provides DATABASE_URL, but SQLModel needs postgresql:// not postgres://
DB_URL = os.getenv("DATABASE_URL", "sqlite:///./mini.db")
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

# Remove check_same_thread for PostgreSQL compatibility
connect_args = {"check_same_thread": False} if "sqlite" in DB_URL else {}
engine = create_engine(DB_URL, connect_args=connect_args)

def init_db():
    SQLModel.metadata.create_all(engine)

def get_session():
    with Session(engine) as session:
        yield session
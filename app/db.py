# app/db.py
import os
from sqlmodel import SQLModel, create_engine, Session

# Railway provides DATABASE_URL, but SQLModel needs postgresql:// not postgres://
DB_URL = os.getenv("DATABASE_URL", "sqlite:///./mini.db")
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

# Remove check_same_thread for PostgreSQL compatibility
connect_args = {"check_same_thread": False} if "sqlite" in DB_URL else {}

# Add connection pooling for PostgreSQL
if "postgresql" in DB_URL:
    engine = create_engine(
        DB_URL,
        connect_args=connect_args,
        pool_pre_ping=True,  # Verify connections before using them
        pool_size=10,  # Number of connections to maintain
        max_overflow=20,  # Additional connections that can be created
        echo=False  # Set to True for SQL debugging
    )
else:
    engine = create_engine(DB_URL, connect_args=connect_args)


def init_db():
    """Create all database tables. If RESET_DB=1, drop existing tables first."""
    import os

    # Check if we should reset the database
    should_reset = os.getenv("RESET_DB", "0").strip().lower() in ("1", "true", "yes")

    if should_reset:
        print("⚠️ RESET_DB=1: Dropping all existing tables...")
        try:
            SQLModel.metadata.drop_all(engine)
            print("✅ All tables dropped")
        except Exception as e:
            print(f"⚠️ Error dropping tables (might not exist): {e}")

    try:
        SQLModel.metadata.create_all(engine)
        print("✅ Database tables created/verified")
    except Exception as e:
        print(f"❌ Error creating tables: {e}")
        raise

def get_session():
    with Session(engine) as session:
        yield session
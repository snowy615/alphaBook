from typing import Optional
import datetime as dt
from sqlmodel import SQLModel, Field

class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    balance: float = Field(default=10000.0)
    is_admin: bool = Field(default=False)
    is_blacklisted: bool = Field(default=False)
    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)

class Order(SQLModel, table=True):
    """Database model for orders - tracks who submitted what and when."""
    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: str = Field(index=True, unique=True)  # UUID from order book
    user_id: int = Field(foreign_key="user.id", index=True)
    symbol: str = Field(index=True)
    side: str  # "BUY" or "SELL"
    price: str  # Store as string to preserve precision
    qty: str  # Original quantity as string
    filled_qty: str = Field(default="0")  # How much has been filled
    status: str = Field(default="OPEN")  # OPEN, FILLED, CANCELED
    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow, index=True)
    updated_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)

class Trade(SQLModel, table=True):
    """Database model for executed trades."""
    id: Optional[int] = Field(default=None, primary_key=True)
    symbol: str = Field(index=True)
    buyer_id: int = Field(foreign_key="user.id", index=True)
    seller_id: int = Field(foreign_key="user.id", index=True)
    price: str  # Execution price as string
    qty: str  # Filled quantity as string
    buy_order_id: str = Field(index=True)  # Reference to buyer's order
    sell_order_id: str = Field(index=True)  # Reference to seller's order
    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow, index=True)

class CustomGame(SQLModel, table=True):
    """Custom trading games created by admins"""
    id: Optional[int] = Field(default=None, primary_key=True)
    symbol: str = Field(index=True, unique=True)  # e.g., "GAME1", "GAME2"
    name: str  # Display name, e.g., "Will it rain tomorrow?"
    instructions: str  # Instructions shown to users
    expected_value: float  # True value used for P&L calculation (hidden from users)
    is_active: bool = Field(default=True)
    is_visible: bool = Field(default=True)  # NEW: Controls visibility on landing page
    created_by: int = Field(foreign_key="user.id")
    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)
    updated_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)

class MarketNews(SQLModel, table=True):
    """Simple market news item that admins can publish."""
    id: Optional[int] = Field(default=None, primary_key=True)
    content: str  # text
    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)

class EquityVisibility(SQLModel, table=True):
    """Track which default equities are visible on landing page"""
    id: Optional[int] = Field(default=None, primary_key=True)
    symbol: str = Field(index=True, unique=True)  # e.g., "AAPL", "MSFT"
    is_visible: bool = Field(default=True)
    updated_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)
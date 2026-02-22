from typing import Optional
import datetime as dt
from pydantic import BaseModel, Field

class User(BaseModel):
    id: Optional[str] = None # Firestore Document ID
    username: str
    password_hash: Optional[str] = None 
    balance: float = Field(default=10000.0)
    is_admin: bool = Field(default=False)
    is_blacklisted: bool = Field(default=False)
    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)
    firebase_uid: Optional[str] = None

class Order(BaseModel):
    id: Optional[str] = None
    order_id: str  # UUID from order book
    user_id: str 
    symbol: str
    side: str  # "BUY" or "SELL"
    price: str  # Store as string to preserve precision
    qty: str  # Original quantity as string
    filled_qty: str = Field(default="0")
    status: str = Field(default="OPEN")
    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)
    updated_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)

class Trade(BaseModel):
    id: Optional[str] = None
    symbol: str
    buyer_id: str
    seller_id: str
    price: str
    qty: str
    buy_order_id: str
    sell_order_id: str
    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)

class CustomGame(BaseModel):
    id: Optional[str] = None
    symbol: str
    name: str 
    instructions: str
    expected_value: float
    game_type: str = Field(default="market")  # "market", "5os", "other"
    is_active: bool = Field(default=True)
    is_visible: bool = Field(default=True)
    is_paused: bool = Field(default=False)
    created_by: str
    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)
    updated_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)

class MarketNews(BaseModel):
    id: Optional[str] = None
    content: str 
    created_at: dt.datetime = Field(default_factory=dt.datetime.utcnow)


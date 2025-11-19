# app/schemas.py
from pydantic import BaseModel, Field
from typing import Literal

class OrderIn(BaseModel):
    # NOTE: user_id is no longer required/accepted from client;
    # we take the logged-in user from the session.
    symbol: str
    side: Literal["BUY", "SELL"]
    price: str = Field(description="Decimal as string")
    qty: str = Field(description="Decimal as string")

class Ack(BaseModel):
    order_id: str
    trades: list[dict]
    snapshot: dict

class PriceOut(BaseModel):
    symbol: str
    price: float | None

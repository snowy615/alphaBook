
from pydantic import BaseModel, Field
from typing import Literal

class OrderIn(BaseModel):
    user_id: str
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

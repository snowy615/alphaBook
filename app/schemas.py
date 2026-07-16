# app/schemas.py
from decimal import Decimal, InvalidOperation
from pydantic import BaseModel, Field, field_validator
from typing import Literal

MAX_PRICE = Decimal("1000000")
MAX_QTY = Decimal("1000000")

class OrderIn(BaseModel):
    # NOTE: user_id is no longer required/accepted from client;
    # we take the logged-in user from the session.
    symbol: str
    side: Literal["BUY", "SELL"]
    price: str = Field(description="Decimal as string")
    qty: str = Field(description="Decimal as string")

    @field_validator("price", "qty")
    @classmethod
    def _positive_decimal(cls, v: str, info):
        try:
            d = Decimal(str(v))
        except (InvalidOperation, ValueError):
            raise ValueError(f"{info.field_name} must be a valid number")
        if not d.is_finite() or d <= 0:
            raise ValueError(f"{info.field_name} must be positive")
        limit = MAX_PRICE if info.field_name == "price" else MAX_QTY
        if d > limit:
            raise ValueError(f"{info.field_name} exceeds the maximum allowed ({limit})")
        return str(d)

class Ack(BaseModel):
    order_id: str
    trades: list[dict]
    snapshot: dict

class PriceOut(BaseModel):
    symbol: str
    price: float | None

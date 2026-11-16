from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Status = Literal["in_transit", "delivered", "exception", "cancelled"]


class ShipmentCreate(BaseModel):
    """Ingest payload. Uses natural keys (SCAC, supplier code, warehouse code) so
    upstream systems never need our surrogate IDs."""

    carrier_scac: str = Field(min_length=2, max_length=4, examples=["NRFX"])
    tracking_number: str = Field(min_length=6, max_length=40, examples=["1234567890"])
    supplier_code: str = Field(examples=["SUP-00042"])
    warehouse_code: str = Field(examples=["DC-ATL"])
    ship_date: date
    promised_date: date
    delivered_date: date | None = None
    status: Status = "in_transit"
    weight_kg: float = Field(gt=0)
    freight_cost_cents: int = Field(ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> "ShipmentCreate":
        # Mirrors the table CHECK constraints so clients get a clear 422 up front;
        # the database constraints remain the source of truth.
        if self.promised_date < self.ship_date:
            raise ValueError("promised_date must be on or after ship_date")
        if self.delivered_date and self.delivered_date < self.ship_date:
            raise ValueError("delivered_date must be on or after ship_date")
        if (self.status == "delivered") != (self.delivered_date is not None):
            raise ValueError("delivered_date is required if and only if status is 'delivered'")
        return self


class ShipmentStatusUpdate(BaseModel):
    status: Status
    delivered_date: date | None = None


class Shipment(BaseModel):
    shipment_id: int
    carrier_scac: str
    tracking_number: str
    supplier_id: int
    supplier_code: str
    warehouse_code: str
    ship_date: date
    promised_date: date
    delivered_date: date | None
    status: Status
    delay_days: int | None
    weight_kg: float
    freight_cost_cents: int


class ShipmentPage(BaseModel):
    items: list[Shipment]
    next_after_id: int | None = Field(description="Pass as after_id to fetch the next page; null when done")

"""请求体模型。"""
from pydantic import BaseModel, Field


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    reason: str = Field(pattern="^(weather|equipment)$")
    event_date: str
    slot_ids: list[int] = Field(min_length=1)
    description: str = ""
    operator: str = "值班经理"


class ResumeRequest(BaseModel):
    manager_name: str = Field(min_length=1)
    confirm: bool


class ActionRequest(BaseModel):
    action_type: str = Field(pattern="^(refund|rebook|waitlist)$")
    idempotency_key: str = Field(min_length=8, max_length=80)
    target_slot_id: int | None = None
    operator: str = "游客"


class PromoteRequest(BaseModel):
    entry_id: int
    slot_id: int
    operator: str = Field(min_length=1, max_length=50)


class AutoPromoteRequest(BaseModel):
    event_id: int
    slot_id: int
    operator: str = Field(min_length=1, max_length=50)


class WindSet(BaseModel):
    speed: float = Field(ge=0, le=60)


class GateVerifyRequest(BaseModel):
    ticket_code: str = Field(min_length=1)
    gate_id: str = "GATE-1"


class PaymentCallbackRequest(BaseModel):
    callback_id: str = Field(min_length=4, max_length=80)
    txn: str = Field(min_length=1)
    result: str = Field(pattern="^(success|failure)$")
    note: str = ""

"""停运事件管理接口。"""
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import EventSlot, Staff, SuspensionEvent, TicketAction
from ..schemas import EventCreate, ResumeRequest
from ..services import create_event, resume_event

router = APIRouter(prefix="/events", tags=["停运事件"])


@router.get("")
def list_events(db: Session = Depends(get_db)):
    evs = db.scalars(select(SuspensionEvent).order_by(SuspensionEvent.id.desc())).all()
    out = []
    for e in evs:
        slot_count = db.scalar(
            select(func.count()).select_from(EventSlot).where(EventSlot.event_id == e.id)
        )
        total = db.scalar(
            select(func.count()).select_from(TicketAction).where(TicketAction.event_id == e.id)
        )
        out.append({
            "id": e.id, "title": e.title, "reason": e.reason,
            "description": e.description, "event_date": e.event_date,
            "status": e.status, "slot_count": slot_count, "action_count": total,
            "created_at": e.created_at.isoformat(sep=" ", timespec="seconds"),
            "resolved_at": e.resolved_at.isoformat(sep=" ", timespec="seconds") if e.resolved_at else None,
            "resumed_by": e.resumed_by,
        })
    return {"events": out}


@router.post("", status_code=201)
def create(req: EventCreate, db: Session = Depends(get_db)):
    """再次停运时调用本接口新建事件；历史事件与处理记录全部保留。"""
    ev = create_event(
        db, title=req.title, reason=req.reason, event_date=req.event_date,
        slot_ids=req.slot_ids, description=req.description, operator=req.operator,
    )
    return {"id": ev.id, "title": ev.title, "status": ev.status}


@router.post("/{event_id}/resume")
def resume(event_id: int, req: ResumeRequest, db: Session = Depends(get_db)):
    """恢复运营：必须由在册设备负责人确认。"""
    ev = resume_event(db, event_id=event_id, manager_name=req.manager_name,
                      confirm=req.confirm)
    return {"id": ev.id, "status": ev.status, "resumed_by": ev.resumed_by}


@router.get("/staff")
def list_staff(role: str | None = None, db: Session = Depends(get_db)):
    q = select(Staff)
    if role:
        q = q.where(Staff.role == role)
    staff = db.scalars(q.order_by(Staff.id)).all()
    return {"staff": [{"id": s.id, "name": s.name, "role": s.role} for s in staff]}

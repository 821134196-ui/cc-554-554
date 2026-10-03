"""调度台接口：看板、递补。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..schemas import AutoPromoteRequest, PromoteRequest
from ..services import auto_promote, get_board, promote_waitlist

router = APIRouter(prefix="/dispatch", tags=["调度"])


@router.get("/board")
def board(event_id: int, db: Session = Depends(get_db)):
    """调度看板：并列展示各时段已售/剩余，候补队列与处理记录。"""
    return get_board(db, event_id)


@router.post("/promote")
def promote(req: PromoteRequest, db: Session = Depends(get_db)):
    """把一条候补递补到指定时段；并发下同一名额只会递补成功一次。"""
    entry = promote_waitlist(db, entry_id=req.entry_id, slot_id=req.slot_id,
                             operator=req.operator)
    return {"id": entry.id, "status": entry.status, "promoted_by": entry.promoted_by}


@router.post("/auto_promote")
def auto(req: AutoPromoteRequest, db: Session = Depends(get_db)):
    """按排队顺序自动递补指定时段的空余名额。"""
    ids = auto_promote(db, event_id=req.event_id, slot_id=req.slot_id,
                       operator=req.operator)
    return {"promoted": ids, "count": len(ids)}

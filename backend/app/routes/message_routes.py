from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import schemas, message_service
from ..database import get_db
from ..auth import get_current_user_id, require_admin

router = APIRouter(prefix="/api/messages", tags=["messages"])


@router.get("/conversations")
def conversations(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    return message_service.conversations(db, user_id)


@router.get("/{other_id}")
def thread_messages(other_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    try:
        return message_service.messages(db, user_id, other_id)
    except message_service.MessageError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("", response_model=schemas.MessageOut)
def send(payload: schemas.MessageSendRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    try:
        msg = message_service.send_message(db, user_id, payload.recipient_id, payload.body)
    except message_service.MessageError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return schemas.MessageOut(
        id=msg.id, sender_id=msg.sender_id, sender_email=msg.sender.email,
        sender_label=msg.sender.label, body=msg.body, is_read=msg.is_read,
        moderated=msg.moderated, created_at=msg.created_at,
    )


@router.post("/{other_id}/read")
def mark_thread_read(other_id: str, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    count = message_service.mark_read(db, user_id, other_id)
    return {"marked_read": count}


@router.post("/block")
def block_user(payload: schemas.BlockRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    try:
        created = message_service.block_user(db, user_id, payload.user_id)
    except message_service.MessageError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"blocked": payload.user_id, "created": created}


@router.delete("/block/{user_id}")
def unblock_user(user_id: str, db: Session = Depends(get_db), blocker_id: str = Depends(get_current_user_id)):
    removed = message_service.unblock_user(db, blocker_id, user_id)
    return {"unblocked": user_id, "removed": removed}


@router.get("/blocked/list")
def list_blocked(db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    return message_service.blocked_users(db, user_id)


@router.post("/report")
def report_user(payload: schemas.ReportRequest, db: Session = Depends(get_db), user_id: str = Depends(get_current_user_id)):
    try:
        rep = message_service.report_user(
            db, user_id, payload.reported_user_id, payload.reason,
            payload.message_id, payload.details,
        )
    except message_service.MessageError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": rep.id, "reason": rep.reason.value if hasattr(rep.reason, "value") else rep.reason}


@router.get("/admin/reports")
def admin_reports(_admin: bool = Depends(require_admin)):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        return message_service.open_reports(db)
    finally:
        db.close()


@router.post("/admin/reports/{report_id}/resolve")
def admin_resolve_report(report_id: str, _admin: bool = Depends(require_admin)):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        ok = message_service.resolve_report(db, report_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Report not found")
        return {"resolved": report_id}
    finally:
        db.close()
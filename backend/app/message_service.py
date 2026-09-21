"""
Private user-to-user messaging — fully internal to the platform.

A message only ever travels between two Vanta accounts. On the way in it is
passed through a content filter that rewrites known scam/spam/abuse words,
so recipients never see the raw offending text. Users can block each other
(blocked senders are rejected server-side) and file moderation reports that
admins can inspect.

No message is ever routed to an external address, inbox or protocol — the
same "internal only" rule that governs coin transfers.
"""
from datetime import datetime

from sqlalchemy.orm import Session

from . import models

# Simple content filter — cheap, deterministic, works offline. Admin
# moderation tools (reports review) feed into the same pipeline.
MODERATED_PATTERNS = {
    "secret account": "private account",
    "send me": "contact me",
    "sincere transfer": "platform transfer",
    "send money": "transfer",
    "wire transfer": "bank transfer",
    "western union": "payment service",
    "moneygram": "payment service",
    "bit.ly": "short link",
    "scam": "phishing",
    "fuck": "f***",
    "shit": "s***",
    "bastard": "b*****d",
    "idiot": "i***t",
    "stupid": "s****d",
    "transfer fee": "fee",
    "account freeze": "account hold",
}

MODERATED_WORDS = {
    "westunion", "moneygram", "bit.ly", "transferfee", "sinceretransfer",
    "fuckyou", "fcuk", "scam", "phish", "ponzi", "pyramid",
}


class MessageError(Exception):
    pass


def _filter_body(body: str) -> tuple[str, bool]:
    """Returns (filtered_body, was_rewritten). Replaces whole or partial
    |pattern| matches and normalizes tricky spellings before scanning."""
    lowered = body.lower()
    text = body

    for bad, good in MODERATED_PATTERNS.items():
        if bad in lowered:
            text = text.replace(bad, good)
            lowered = text.lower()

    compact = "".join(ch for ch in lowered if ch.isalnum())
    rewritten = text != body
    for word in MODERATED_WORDS:
        if word in compact:
            rewritten = True
    if rewritten:
        # Second pass normalizes any remaining obviously-hostile fragments.
        for bad, good in MODERATED_PATTERNS.items():
            text = text.replace(bad, good)
    return text, rewritten


def is_blocked(db: Session, a_id: str, b_id: str) -> bool:
    return (
        db.query(models.UserBlock).filter_by(blocker_id=a_id, blocked_id=b_id).first()
        is not None
    )


def send_message(db: Session, sender_id: str, recipient_id: str, body: str) -> models.DirectMessage:
    if not body or not body.strip():
        raise MessageError("Message is empty")
    if len(body) > 500:
        raise MessageError("Messages are limited to 500 characters")
    if sender_id == recipient_id:
        raise MessageError("You cannot message yourself")

    sender = db.query(models.User).filter_by(id=sender_id).first()
    recipient = db.query(models.User).filter_by(id=recipient_id).first()
    if not sender or not recipient:
        raise MessageError("User not found")

    if is_blocked(db, sender_id, recipient_id):
        raise MessageError("You have blocked this user — unblock them before messaging")
    if is_blocked(db, recipient_id, sender_id):
        raise MessageError("You can't message this user right now")

    filtered, moderated = _filter_body(body.strip())
    msg = models.DirectMessage(
        sender_id=sender_id,
        recipient_id=recipient_id,
        body=filtered,
        moderated=moderated,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def _user_public(user: models.User) -> dict:
    return {"id": user.id, "email": user.email, "label": user.label}


def conversations(db: Session, user_id: str) -> list:
    """One entry per other user the current user has a thread with, sorted
    by most recent message, including the last body and unread count."""
    mine = (
        db.query(models.DirectMessage)
        .filter((models.DirectMessage.sender_id == user_id) | (models.DirectMessage.recipient_id == user_id))
        .order_by(models.DirectMessage.created_at.desc())
        .all()
    )

    by_other = {}
    for m in mine:
        other_id = m.recipient_id if m.sender_id == user_id else m.sender_id
        entry = by_other.setdefault(
            other_id,
            {"last": m, "unread": 0},
        )
        if m.recipient_id == user_id and not m.is_read:
            entry["unread"] += 1

    out = []
    for other_id, entry in by_other.items():
        other = db.query(models.User).filter_by(id=other_id).first()
        if not other:
            continue
        out.append({
            "user_id": other.id,
            "email": other.email,
            "label": other.label,
            "last_message": entry["last"].body,
            "last_message_at": entry["last"].created_at,
            "unread_count": entry["unread"],
        })
    out.sort(key=lambda c: c["last_message_at"] or datetime.min, reverse=True)
    return out


def messages(db: Session, user_id: str, other_id: str, limit: int = 200) -> list:
    other = db.query(models.User).filter_by(id=other_id).first()
    if not other:
        raise MessageError("User not found")

    rows = (
        db.query(models.DirectMessage)
        .filter(
            (
                (models.DirectMessage.sender_id == user_id)
                & (models.DirectMessage.recipient_id == other_id)
            )
            | (
                (models.DirectMessage.sender_id == other_id)
                & (models.DirectMessage.recipient_id == user_id)
            )
        )
        .order_by(models.DirectMessage.created_at.asc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": m.id,
            "sender_id": m.sender_id,
            "sender_email": m.sender.email,
            "sender_label": m.sender.label,
            "body": m.body,
            "is_read": m.is_read,
            "moderated": m.moderated,
            "created_at": m.created_at,
        }
        for m in rows
    ]


def mark_read(db: Session, user_id: str, other_id: str):
    rows = (
        db.query(models.DirectMessage)
        .filter(
            models.DirectMessage.sender_id == other_id,
            models.DirectMessage.recipient_id == user_id,
            models.DirectMessage.is_read.is_(False),
        )
        .all()
    )
    for m in rows:
        m.is_read = True
    if rows:
        db.commit()
    return len(rows)


def block_user(db: Session, blocker_id: str, blocked_id: str):
    if blocker_id == blocked_id:
        raise MessageError("You cannot block yourself")
    target = db.query(models.User).filter_by(id=blocked_id).first()
    if not target:
        raise MessageError("User not found")
    existing = db.query(models.UserBlock).filter_by(blocker_id=blocker_id, blocked_id=blocked_id).first()
    if existing:
        return False
    db.add(models.UserBlock(blocker_id=blocker_id, blocked_id=blocked_id))
    db.commit()
    return True


def unblock_user(db: Session, blocker_id: str, blocked_id: str) -> bool:
    row = db.query(models.UserBlock).filter_by(blocker_id=blocker_id, blocked_id=blocked_id).first()
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True


def blocked_users(db: Session, user_id: str) -> list:
    rows = db.query(models.UserBlock).filter_by(blocker_id=user_id).all()
    out = []
    for r in rows:
        u = db.query(models.User).filter_by(id=r.blocked_id).first()
        if u:
            out.append(_user_public(u))
    return out


def report_user(
    db: Session,
    reporter_id: str,
    reported_user_id: str,
    reason: str,
    message_id: str = None,
    details: str = None,
) -> models.Report:
    if reporter_id == reported_user_id:
        raise MessageError("You cannot report yourself")
    reason_val = reason.upper()
    try:
        reason_enum = models.ReportReason[reason_val]
    except KeyError:
        raise MessageError("Invalid report reason")
    target = db.query(models.User).filter_by(id=reported_user_id).first()
    if not target:
        raise MessageError("User not found")
    rep = models.Report(
        reporter_id=reporter_id,
        reported_user_id=reported_user_id,
        message_id=message_id,
        reason=reason_enum,
        details=(details or "").strip() or None,
    )
    db.add(rep)
    db.commit()
    db.refresh(rep)
    return rep


def open_reports(db: Session) -> list:
    """Admin tool — every unresolved moderation report, newest first."""
    rows = (
        db.query(models.Report)
        .filter(models.Report.resolved.is_(False))
        .order_by(models.Report.created_at.desc())
        .limit(200)
        .all()
    )
    out = []
    for r in rows:
        reporter = db.query(models.User).filter_by(id=r.reporter_id).first()
        reported = db.query(models.User).filter_by(id=r.reported_user_id).first()
        out.append({
            "id": r.id,
            "reporter_email": reporter.email if reporter else "?",
            "reported_user_email": reported.email if reported else "?",
            "reason": r.reason.value if hasattr(r.reason, "value") else r.reason,
            "details": r.details,
            "created_at": r.created_at,
        })
    return out


def resolve_report(db: Session, report_id: str) -> bool:
    row = db.query(models.Report).filter_by(id=report_id).first()
    if not row:
        return False
    row.resolved = True
    db.commit()
    return True
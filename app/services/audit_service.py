import json
from datetime import datetime
from flask import request
from app.database import db
from app.models import AuditLog

def log_audit(action, entity_type=None, entity_id=None, details=None, user_id=None):
    """
    Records an immutable audit log entry.
    """
    try:
        ip = request.remote_addr if request else "127.0.0.1"
    except Exception:
        ip = "127.0.0.1"

    details_str = json.dumps(details, default=str) if details else None

    entry = AuditLog(
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id else None,
        ip_address=ip,
        details_json=details_str,
        created_at=datetime.utcnow()
    )
    db.session.add(entry)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return entry

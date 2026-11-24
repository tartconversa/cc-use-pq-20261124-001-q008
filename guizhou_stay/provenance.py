from __future__ import annotations

from datetime import datetime, timezone
import json
from sqlite3 import Connection

from guizhou_stay.contracts import Evidence
from guizhou_stay.digest import digest
from guizhou_stay.errors import conflict, invalid, missing
from guizhou_stay.storage import dump


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def record(db: Connection, body: Evidence) -> dict:
    payload = body.model_dump(mode="json")
    value = digest(payload)
    old = db.execute("SELECT digest FROM evidence WHERE evidence_id=?", (body.evidence_id,)).fetchone()
    if old:
        if old["digest"] != value:
            raise conflict("evidence_changed", "证明编号已对应另一份内容")
        return {**payload, "replayed": True, "digest": value}
    supply = db.execute(
        "SELECT payload FROM supplies WHERE item_id=? AND source=? AND source_revision=?",
        (body.supply_id, body.source, body.source_revision),
    ).fetchone()
    if supply is None:
        raise missing("supply", body.supply_id)
    if json.loads(supply["payload"])["category"] != body.category:
        raise invalid("evidence_category", "证明类别与供给类别不一致")
    db.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (body.evidence_id, body.supply_id, body.category, body.issuer, body.issued_at.isoformat(),
                body.expires_at.isoformat(), body.source, body.source_revision, body.document_ref, body.status, value))
    return {**payload, "replayed": False, "digest": value}


def active_for(db: Connection, supply_id: str, category: str, at: datetime, *, source: str | None = None, revision: int | None = None) -> list[Evidence]:
    query = "SELECT * FROM evidence WHERE supply_id=? AND category=? AND status='verified' AND issued_at<=? AND expires_at>?"
    args: list = [supply_id, category, at.isoformat(), at.isoformat()]
    if revision is not None:
        query += " AND source_revision=?"
        args.append(revision)
    if source is not None:
        query += " AND source=?"
        args.append(source)
    rows = db.execute(query + " ORDER BY expires_at DESC,evidence_id", args).fetchall()
    return [Evidence.model_validate({key: row[key] for key in Evidence.model_fields}) for row in rows]


def list_evidence(db: Connection, supply_id: str | None = None) -> list[dict]:
    query = "SELECT evidence_id,supply_id,category,issuer,issued_at,expires_at,source,source_revision,document_ref,status,digest FROM evidence"
    args = []
    if supply_id:
        query += " WHERE supply_id=?"
        args.append(supply_id)
    return [dict(row) for row in db.execute(query + " ORDER BY expires_at,evidence_id", args)]


def evidence_at(db: Connection, evidence_id: str, at: datetime) -> Evidence:
    row = db.execute("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
    if row is None:
        raise missing("evidence", evidence_id)
    item = Evidence.model_validate({key: row[key] for key in Evidence.model_fields})
    if not item.issued_at <= at < item.expires_at:
        raise conflict("evidence_expired", "证明在指定时间点无效")
    return item


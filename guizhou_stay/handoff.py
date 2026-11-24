from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import Field

from guizhou_stay import exporter
from guizhou_stay.contracts import Key, StrictModel
from guizhou_stay.digest import digest
from guizhou_stay.errors import conflict, invalid, missing


class Delivery(StrictModel):
    destination: Key
    delivery_id: Key
    package_id: Key
    edition: Annotated[int, Field(ge=1, le=1_000_000)]
    expected_sequence: Annotated[int, Field(ge=0, le=1_000_000)]


class Receipt(StrictModel):
    delivery_id: Key
    package_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    receiver_reference: Key


def stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def last(db, destination: str):
    return db.execute(
        """SELECT * FROM deliveries WHERE destination=?
           ORDER BY sequence DESC LIMIT 1""", (destination,),
    ).fetchone()


def entry(db, destination: str, delivery_id: str):
    row = db.execute(
        "SELECT * FROM deliveries WHERE destination=? AND delivery_id=?",
        (destination, delivery_id),
    ).fetchone()
    if row is None:
        raise missing("delivery", delivery_id)
    return row


def public(row) -> dict:
    keys = ("destination", "delivery_id", "package_id", "edition", "sequence",
            "package_digest", "queued_at", "acknowledged_at", "receiver_reference")
    return {key: row[key] for key in keys}


def queue(db, body: Delivery) -> dict:
    fingerprint = digest(body.model_dump(mode="json"))
    duplicate = db.execute(
        "SELECT * FROM deliveries WHERE destination=? AND delivery_id=?",
        (body.destination, body.delivery_id),
    ).fetchone()
    if duplicate is not None:
        if duplicate["request_digest"] != fingerprint:
            raise conflict("delivery_changed", "发件编号已经绑定其他封存内容")
        return public(duplicate)
    previous = last(db, body.destination)
    previous_sequence = previous["sequence"] if previous else 0
    if body.expected_sequence != previous_sequence:
        raise conflict("destination_sequence", "目的渠道已经有新的出件记录",
                       {"sequence": previous_sequence})
    if previous is not None and previous["acknowledged_at"] is None:
        raise conflict("delivery_unconfirmed", "目的渠道尚未回执上一项供给包",
                       {"delivery_id": previous["delivery_id"]})
    package = exporter.package_document(db, body.package_id, body.edition)
    if package["state"] != "published":
        raise invalid("delivery_draft", "只有通过覆盖检查的封存包可以出件")
    delivered = db.execute(
        """SELECT MAX(edition) FROM deliveries WHERE destination=? AND package_id=?""",
        (body.destination, body.package_id),
    ).fetchone()[0]
    if delivered is not None and body.edition <= delivered:
        raise conflict("delivery_edition", "目的渠道已收到相同或更高版次",
                       {"edition": delivered})
    db.execute(
        """INSERT INTO deliveries(destination,delivery_id,package_id,edition,sequence,
           package_digest,request_digest,queued_at) VALUES(?,?,?,?,?,?,?,?)""",
        (body.destination, body.delivery_id, body.package_id, body.edition,
         previous_sequence + 1, package["digest"], fingerprint, stamp()),
    )
    return public(entry(db, body.destination, body.delivery_id))


def acknowledge(db, destination: str, receipt: Receipt) -> dict:
    row = entry(db, destination, receipt.delivery_id)
    if receipt.package_digest != row["package_digest"]:
        raise conflict("receipt_digest", "渠道回执与实际发送的供给包摘要不一致")
    if row["acknowledged_at"] is not None:
        if receipt.receiver_reference != row["receiver_reference"]:
            raise conflict("receipt_changed", "渠道出件已经获得另一项确认")
        return public(row)
    duplicate_ref = db.execute(
        """SELECT 1 FROM deliveries WHERE destination=? AND receiver_reference=?""",
        (destination, receipt.receiver_reference),
    ).fetchone()
    if duplicate_ref:
        raise conflict("receipt_reused", "渠道回执号已经确认了另一份交接")
    db.execute(
        """UPDATE deliveries SET acknowledged_at=?, receiver_reference=?
           WHERE destination=? AND delivery_id=?""",
        (stamp(), receipt.receiver_reference, destination, receipt.delivery_id),
    )
    return public(entry(db, destination, receipt.delivery_id))


def history(db, destination: str, since: int, limit: int) -> dict:
    entries = db.execute(
        """SELECT * FROM deliveries WHERE destination=? AND sequence>?
           ORDER BY sequence LIMIT ?""", (destination, since, limit + 1),
    ).fetchall()
    return {
        "destination": destination, "items": [public(row) for row in entries[:limit]],
        "next_sequence": entries[limit - 1]["sequence"] if len(entries) > limit else None,
    }


def awaiting(db, destination: str) -> dict:
    row = last(db, destination)
    task = public(row) if row is not None and row["acknowledged_at"] is None else None
    return {"destination": destination, "pending": task}


def package_for_delivery(db, destination: str, delivery_id: str) -> dict:
    served = entry(db, destination, delivery_id)
    document = exporter.package_document(db, served["package_id"], served["edition"])
    if document["digest"] != served["package_digest"]:
        raise RuntimeError("供给包与交接记录失去一致性")
    return document

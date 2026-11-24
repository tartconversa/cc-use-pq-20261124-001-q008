from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from sqlite3 import Connection
import unicodedata

from pydantic import ValidationError

from guizhou_stay.contracts import Page, Supply
from guizhou_stay.digest import digest
from guizhou_stay.errors import conflict, invalid, missing
from guizhou_stay.storage import dump


def stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def canonical_key(item: Supply) -> str:
    return dump([normalize_text(value) for value in
                 (item.kind, item.provider_id, item.title, item.city, item.region)])


def identity_id(item: Supply) -> str:
    return "id-" + digest(canonical_key(item))[:24]


def gather_rows(body: Page) -> list[tuple[Supply, str]]:
    rows = {}
    for position, raw in enumerate(body.rows, start=1):
        try:
            item = Supply.model_validate({**raw, "source": body.source,
                                          "source_revision": body.source_revision})
        except ValidationError as exc:
            raise invalid("supply_row", "页面中的供给卡片不符合合同",
                          {"position": position,
                           "fields": [error["loc"][0] for error in exc.errors() if error["loc"]]}) from exc
        fingerprint = digest(item.model_dump(mode="json"))
        previous = rows.get(item.item_id)
        if previous is not None and fingerprint != previous[1]:
            raise conflict("supply_changed", "同一快照存在不同的同号供给卡片",
                           {"item_id": item.item_id, "position": position})
        rows[item.item_id] = (item, fingerprint)
    return list(rows.values())


def ingest_page(db: Connection, body: Page) -> dict:
    rows = gather_rows(body)
    for item, fingerprint in rows:
        previous = db.execute(
            """SELECT digest,page_no FROM staged WHERE session_id=? AND item_id=?
               ORDER BY page_no LIMIT 1""", (body.session_id, item.item_id)
        ).fetchone()
        if previous is not None and fingerprint != previous["digest"]:
            raise conflict("supply_changed", "跨页重复卡片的内容不一致",
                           {"item_id": item.item_id, "previous_page": previous["page_no"]})
        db.execute("INSERT INTO staged VALUES(?,?,?,?,?)",
                   (body.session_id, body.page_no, item.item_id, fingerprint,
                    dump(item.model_dump(mode="json"))))
    if body.has_next:
        return {"accepted": len(rows), "repeated": 0, "visible": False,
                "source_revision": body.source_revision}
    result = promote(db, body.session_id)
    return {**result, "visible": True, "source_revision": body.source_revision}


def promote(db: Connection, session_id: str) -> dict:
    staged = db.execute(
        """SELECT item_id,page_no,digest,payload FROM staged WHERE session_id=?
           ORDER BY page_no,item_id""", (session_id,)
    )
    accepted = 0
    repeated = 0
    for row in staged:
        item = Supply.model_validate_json(row["payload"])
        fingerprint = row["digest"]
        if record_supply(db, item, fingerprint, row["payload"]):
            accepted += 1
        else:
            repeated += 1
        db.execute("INSERT INTO observations VALUES(?,?,?,?,?)",
                   (item.item_id, item.source, item.source_revision, session_id, row["page_no"]))
    return {"accepted": accepted, "repeated": repeated}


def record_supply(db, item: Supply, fingerprint: str, payload: str) -> bool:
    old = db.execute(
        """SELECT digest FROM supplies WHERE item_id=? AND source=? AND source_revision=?""",
        (item.item_id, item.source, item.source_revision),
    ).fetchone()
    if old is not None:
        if old["digest"] != fingerprint:
            raise conflict("supply_changed", "另一完整采集已登记不同的同号同修订卡片",
                           {"item_id": item.item_id, "source": item.source})
        return False
    db.execute("INSERT INTO supplies VALUES(?,?,?,?,?,?)",
               (item.item_id, item.source, item.source_revision, fingerprint, payload, stamp()))
    _attach_identity(db, item, fingerprint)
    return True


def _attach_identity(db: Connection, item: Supply, item_digest: str) -> str:
    identity = identity_id(item)
    db.execute(
        """INSERT INTO identities VALUES(?,?,?,?,?,?,?) ON CONFLICT(identity_id) DO NOTHING""",
        (identity, item.kind, canonical_key(item), "[]", item_digest, item.status, stamp()),
    )
    db.execute("INSERT INTO identity_sources VALUES(?,?,?,?,?)",
               (identity, item.item_id, item.source, item.source_revision, item_digest))
    return identity


def supply(db: Connection, item_id: str, source: str, revision: int) -> Supply:
    row = db.execute(
        "SELECT payload FROM supplies WHERE item_id=? AND source=? AND source_revision=?",
        (item_id, source, revision),
    ).fetchone()
    if row is None:
        raise missing("supply", f"{source}/{item_id}/{revision}")
    return Supply.model_validate_json(row["payload"])


def versions(db: Connection, *, revision: int | None = None) -> list[Supply]:
    if revision is not None:
        rows = db.execute("SELECT payload FROM supplies WHERE source_revision=? ORDER BY source,item_id",
                          (revision,))
    else:
        rows = db.execute(
            """WITH latest AS (
               SELECT source,item_id,MAX(source_revision) AS source_revision FROM supplies
               GROUP BY source,item_id
               )
               SELECT supplies.payload FROM supplies JOIN latest USING(source,item_id,source_revision)
               ORDER BY source,item_id"""
        )
    return [Supply.model_validate_json(row["payload"]) for row in rows]


@dataclass(frozen=True)
class Group:
    identity_id: str
    rows: list[Supply]

    @property
    def winner(self) -> Supply:
        return min(self.rows, key=lambda item: (-item.updated_at.timestamp(),
                                                item.source, item.item_id))

    def disputed_fields(self) -> list[str]:
        fields = ("category", "status", "address_hint", "price_band", "service_window")
        return [key for key in fields if len({getattr(item, key) for item in self.rows}) > 1]

    def public(self) -> dict:
        return {
            "identity_id": self.identity_id,
            "canonical": canonical_key(self.winner),
            "kind": self.winner.kind,
            "aliases": sorted({item.item_id for item in self.rows}),
            "status": self.winner.status,
            "current_digest": digest(self.winner.model_dump(mode="json")),
            "disputed_fields": self.disputed_fields(),
            "sources": [
                {"source": item.source, "item_id": item.item_id, "revision": item.source_revision}
                for item in self.rows
            ],
        }


def groups(db: Connection, *, region: str | None = None, revision: int | None = None) -> list[Group]:
    index = {}
    for item in versions(db, revision=revision):
        if region is None or item.region == region:
            index.setdefault(identity_id(item), []).append(item)
    return [Group(key, values) for key, values in sorted(index.items())]


def identities(db: Connection, *, kind: str | None = None, region: str | None = None) -> list[dict]:
    return [group.public() for group in groups(db, region=region)
            if kind is None or group.winner.kind == kind]


def current_supplies(db: Connection, *, region: str | None = None,
                     source_revision: int | None = None) -> list[Supply]:
    return [group.winner for group in groups(db, region=region, revision=source_revision)]


def receipt_sources(db: Connection, item: Supply) -> list[dict]:
    rows = db.execute(
        """SELECT o.session_id,o.page_no,p.page_digest,p.captured_at,c.html_digest,c.locator
           FROM observations AS o JOIN pages AS p USING(session_id,page_no)
           LEFT JOIN captures AS c USING(session_id,page_no)
           WHERE o.item_id=? AND o.source=? AND o.source_revision=?
           ORDER BY o.session_id,o.page_no""",
        (item.item_id, item.source, item.source_revision),
    )
    return [dict(row) for row in rows]

from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import Connection

from guizhou_stay.catalog import Group, groups, receipt_sources, versions
from guizhou_stay.digest import digest
from guizhou_stay.errors import invalid, missing

FIELDS = ("title", "provider_id", "city", "region", "category", "status",
          "address_hint", "price_band", "service_window", "tags")


@dataclass(frozen=True)
class Difference:
    identity: str
    before: Group | None
    after: Group | None

    def payload(self) -> dict:
        previous = self.before.winner if self.before else None
        current = self.after.winner if self.after else None
        changes = {}
        if previous is not None and current is not None:
            for key in FIELDS:
                left, right = getattr(previous, key), getattr(current, key)
                if left != right:
                    changes[key] = {"before": left, "after": right}
        return {
            "identity_id": self.identity,
            "change": "added" if previous is None else "removed" if current is None else "updated",
            "fields": changes,
            "before": self.before.public() if self.before else None,
            "after": self.after.public() if self.after else None,
        }


def material_changes(before: list[Group], after: list[Group]) -> list[dict]:
    old = {value.identity_id: value for value in before}
    new = {value.identity_id: value for value in after}
    output = []
    for key in sorted(old.keys() | new.keys()):
        previous, current = old.get(key), new.get(key)
        if previous is not None and current is not None:
            left = previous.winner
            right = current.winner
            no_content_change = all(getattr(left, field) == getattr(right, field) for field in FIELDS)
            if (no_content_change and previous.disputed_fields() == current.disputed_fields()
                    and {(item.source, item.item_id) for item in previous.rows}
                    == {(item.source, item.item_id) for item in current.rows}):
                continue
        output.append(Difference(key, previous, current).payload())
    return output


def diff_revisions(db: Connection, before: int, after: int, region: str | None = None) -> dict:
    if before >= after:
        raise invalid("revision_order", "来源修订必须按时间向前比较")
    left = groups(db, region=region, revision=before)
    right = groups(db, region=region, revision=after)
    changes = material_changes(left, right)
    return {"before": before, "after": after, "region": region,
            "before_identities": len(left), "after_identities": len(right),
            "changes": changes}


def history(db: Connection, item_id: str) -> list[dict]:
    rows = db.execute(
        """SELECT payload,digest,received_at FROM supplies WHERE item_id=?
           ORDER BY source,source_revision""", (item_id,),
    ).fetchall()
    if not rows:
        raise missing("supply", item_id)
    from guizhou_stay.contracts import Supply

    result = []
    for row in rows:
        item = Supply.model_validate_json(row["payload"])
        result.append({
            "item": item.model_dump(mode="json"), "digest": row["digest"],
            "received_at": row["received_at"], "pages": receipt_sources(db, item),
        })
    return result


def source_inventory(db: Connection, revision: int, region: str) -> dict:
    observations = {}
    for item in versions(db, revision=revision):
        if item.region != region:
            continue
        source = observations.setdefault(item.source, {"references": [], "items": []})
        source["items"].append(item.item_id)
        source["references"].extend(
            (page["session_id"], page["page_no"], page["page_digest"])
            for page in receipt_sources(db, item)
        )
    result = {}
    for source, value in observations.items():
        refs = sorted(set(value["references"]))
        result[source] = {
            "items": sorted(value["items"]),
            "page_count": len(refs),
            "session_count": len({ref[0] for ref in refs}),
            "trace_digest": digest(refs),
        }
    return result

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from sqlite3 import Connection

from guizhou_stay import plans
from guizhou_stay.catalog import Group, groups, receipt_sources
from guizhou_stay.contracts import CoveragePolicy, PublishRequest
from guizhou_stay.digest import digest
from guizhou_stay.errors import conflict, invalid, missing
from guizhou_stay.provenance import active_for
from guizhou_stay.storage import dump


def normalized_time(at: datetime) -> datetime:
    if at.tzinfo is None:
        raise invalid("evaluation_time", "评估时间必须包含时区")
    return at.astimezone(timezone.utc).replace(microsecond=0)


def inspect_group(db: Connection, group: Group, policy: CoveragePolicy, at: datetime, sessions=None) -> dict:
    item = group.winner
    gaps = group.disputed_fields()
    witnesses = []
    rejections = []
    for version in group.rows:
        sources = scoped_receipts(db, version, sessions)
        first_seen = min((source["captured_at"] for source in sources), default=None)
        if first_seen is None or datetime.fromisoformat(first_seen) > at:
            rejections.append("unobserved_at")
            continue
        if version.updated_at > at:
            rejections.append("future_supply")
            continue
        if at - version.updated_at > timedelta(days=policy.max_source_age_days):
            rejections.append("stale_source")
            continue
        proofs = active_for(db, version.item_id, version.category, at,
                            source=version.source, revision=version.source_revision)
        witnesses.append({
            "item": version.model_dump(mode="json"),
            "proofs": [proof.model_dump(mode="json") for proof in proofs],
            "captures": [source for source in sources
                         if datetime.fromisoformat(source["captured_at"]) <= at],
        })
    if not witnesses:
        state = "unavailable"
    elif gaps:
        state = "disputed"
    elif item.status != "active":
        state = item.status
    elif any(witness["proofs"] for witness in witnesses):
        state = "verified"
    else:
        state = "unverified"
    return {
        "identity_id": group.identity_id, "item_id": item.item_id, "category": item.category,
        "state": state, "disputed_fields": gaps, "reasons": sorted(set(rejections)),
        "witnesses": witnesses,
    }


def scoped_receipts(db, item, sessions: set[str] | None):
    evidence = receipt_sources(db, item)
    return [row for row in evidence if sessions is None or row["session_id"] in sessions]


def as_of_groups(db, region: str, revision: int, at: datetime, sessions=None):
    for group in groups(db, region=region, revision=revision):
        visible = [
            item for item in group.rows
            if item.updated_at <= at and any(
                datetime.fromisoformat(source["captured_at"]) <= at
                for source in scoped_receipts(db, item, sessions)
            )
        ]
        if visible:
            yield Group(group.identity_id, visible)


def assess(db: Connection, region: str, policy: CoveragePolicy, at: datetime, revision: int, plan_id: str | None = None) -> dict:
    at = normalized_time(at)
    plan_report = plans.readiness(db, plan_id, at) if plan_id else None
    if plan_report and plan_report["region"] != region:
        raise invalid("plan_region", "采集计划与供给地区不一致")
    if plan_report and any(target["target"]["source_revision"] != revision for target in plan_report["targets"]):
        raise invalid("plan_revision", "评估来源修订必须与计划目标一致")
    sessions = plans.included_sessions(plan_report) if plan_report else None
    categorized = {name: [] for name in policy.required_categories}
    for group in as_of_groups(db, region, revision, at, sessions):
        finding = inspect_group(db, group, policy, at, sessions)
        if finding["category"] in categorized:
            categorized[finding["category"]].append(finding)
    categories = {}
    gaps = []
    for category, findings in categorized.items():
        states = {key: [] for key in ("verified", "unverified", "pending", "withdrawn",
                                       "unavailable", "disputed")}
        for finding in findings:
            states[finding["state"]].append(finding["item_id"])
        eligible = list(states["verified"])
        if not policy.require_verified_evidence:
            eligible.extend(states["unverified"])
        if len(eligible) < policy.minimum_active_items:
            gaps.append({"category": category, "reason": "minimum_active_items",
                         "observed": len(eligible), "minimum": policy.minimum_active_items})
        if states["disputed"]:
            gaps.append({"category": category, "reason": "source_conflict",
                         "items": states["disputed"]})
        if states["pending"] and not policy.allow_pending:
            gaps.append({"category": category, "reason": "pending_evidence",
                         "items": states["pending"]})
        categories[category] = {**states, "active": states["verified"] + states["unverified"],
                                "eligible": eligible, "findings": findings}
    return {
        "region": region, "revision": revision, "as_of": at.isoformat(),
        "categories": categories, "gaps": gaps, "plan": plan_report,
        "ready": not gaps and (plan_report is None or plan_report["ready"]),
        "active_items": sum(len(entry["active"]) for entry in categories.values()),
    }


def latest_revision(db: Connection, package_id: str) -> int:
    return db.execute("SELECT COALESCE(MAX(revision),0) FROM packages WHERE package_id=?",
                      (package_id,)).fetchone()[0]


def create_package(db: Connection, request: PublishRequest) -> dict:
    request_digest = digest(request.model_dump(mode="json"))
    existing = db.execute("SELECT document FROM packages WHERE package_id=? AND request_digest=?",
                          (request.package_id, request_digest)).fetchone()
    if existing is not None:
        return json.loads(existing["document"])
    previous = latest_revision(db, request.package_id)
    if request.expected_package_revision is not None and previous != request.expected_package_revision:
        raise conflict("package_revision", "供给包已有更新，请重新预览",
                       {"current_revision": previous})
    assessment = assess(db, request.region, request.policy, request.requested_at, request.expected_revision, request.plan_id)
    if request.preview_digest is not None and request.preview_digest != digest(assessment):
        raise conflict("preview_changed", "采集或证明在预览后发生变化，请重新核对")
    state = "published" if assessment["ready"] else "draft"
    document = {
        "package_id": request.package_id, "revision": previous + 1,
        "region": request.region, "requested_at": assessment["as_of"],
        "source_revision": request.expected_revision, "policy": request.policy.model_dump(mode="json"),
        "state": state, "items": assessment["categories"], "gaps": assessment["gaps"],
        "assessment": assessment,
    }
    document["digest"] = digest(document)
    db.execute(
        """INSERT INTO packages(package_id,revision,region,policy,items,gaps,digest,state,
           created_at,request_digest,document) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (request.package_id, previous + 1, request.region, dump(document["policy"]),
         dump(document["items"]), dump(document["gaps"]), document["digest"], state,
         document["requested_at"], request_digest, dump(document)),
    )
    return document


def package(db: Connection, package_id: str, revision: int | None = None) -> dict:
    if revision is None:
        revision = latest_revision(db, package_id)
    row = db.execute("SELECT document FROM packages WHERE package_id=? AND revision=?",
                     (package_id, revision)).fetchone()
    if row is None:
        raise missing("package", package_id)
    return json.loads(row["document"])


def compare(db: Connection, package_id: str, before: int, after: int) -> dict:
    if before >= after:
        raise invalid("revision_order", "旧版必须早于新版")
    first = package(db, package_id, before)
    second = package(db, package_id, after)
    old, new = first["items"], second["items"]
    changes = []
    for category in sorted(old.keys() | new.keys()):
        if old.get(category) != new.get(category):
            changes.append({"category": category, "before": old.get(category), "after": new.get(category)})
    return {
        "package_id": package_id, "before": before, "after": after, "changes": changes,
        "before_state": first["state"], "after_state": second["state"],
        "before_digest": first["digest"], "after_digest": second["digest"],
    }

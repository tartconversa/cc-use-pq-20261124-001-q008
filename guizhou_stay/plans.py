from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Annotated, Literal

from pydantic import Field, model_validator

from guizhou_stay.contracts import Key, Text, StrictModel
from guizhou_stay.digest import digest
from guizhou_stay.errors import DomainError, conflict, invalid, missing
from guizhou_stay.storage import dump

Cut = Annotated[int, Field(ge=1, le=1_000_000)]


class Target(StrictModel):
    slot: Key
    source: Key
    query: Text
    locale: Text = "zh-HK"
    source_revision: Cut
    necessity: Literal["required", "optional"] = "required"
    allow_empty: bool = False


class NewPlan(StrictModel):
    plan_id: Key
    region: Text
    max_capture_age_hours: Annotated[int, Field(ge=1, le=720)] = 72
    targets: list[Target] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_targets(self):
        slots = [target.slot for target in self.targets]
        scopes = [(t.source, t.query, t.locale) for t in self.targets]
        if len(slots) != len(set(slots)) or len(scopes) != len(set(scopes)):
            raise ValueError("采集槽及目标查询不得重复")
        if not any(t.necessity == "required" for t in self.targets):
            raise ValueError("需要至少一个必采目标")
        return self


class Assignment(StrictModel):
    session_id: Key
    expected_generation: Annotated[int, Field(ge=0, le=1_000_000)]


def clock() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def register(db, data: NewPlan) -> dict:
    payload = data.model_dump(mode="json")
    payload["targets"].sort(key=lambda row: row["slot"])
    fingerprint = digest(payload)
    old = db.execute("SELECT digest FROM plans WHERE plan_id=?", (data.plan_id,)).fetchone()
    if old is not None:
        if old["digest"] != fingerprint:
            raise conflict("plan_changed", "计划编号已经固定了另一个采集范围")
        return read(db, data.plan_id)
    db.execute("INSERT INTO plans VALUES(?,?,?,?)",
               (data.plan_id, fingerprint, dump(payload), clock()))
    return read(db, data.plan_id)


def load(db, plan_id: str) -> NewPlan:
    row = db.execute("SELECT document FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
    if row is None:
        raise missing("plan", plan_id)
    return NewPlan.model_validate_json(row["document"])


def head(db, plan_id, slot):
    return db.execute(
        """SELECT generation,session_id FROM assignments WHERE plan_id=? AND slot=?
           ORDER BY generation DESC LIMIT 1""", (plan_id, slot),
    ).fetchone()


def validate_selection(db, target: Target, session_id: str):
    row = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if row is None:
        raise missing("session", session_id)
    fields = ("source", "query", "locale", "source_revision")
    mismatch = [key for key in fields if row[key] != getattr(target, key)]
    if mismatch:
        raise conflict("plan_scope", "会话来自不同的来源、检索范围或修订", {"fields": mismatch})
    if row["state"] != "complete":
        raise conflict("plan_unfinished", "只能选用通过全页确认的采集会话")
    pages = db.execute(
        """SELECT p.page_no,p.page_digest,p.captured_at,p.payload,c.html_digest
           FROM pages AS p LEFT JOIN captures AS c USING(session_id,page_no)
           WHERE session_id=? ORDER BY p.page_no""", (session_id,),
    ).fetchall()
    if len(pages) != row["expected_pages"]:
        raise conflict("plan_page_count", "采集会话缺少页面凭证")
    valid_rows = sum(len(json.loads(page["payload"])["rows"]) for page in pages)
    if not valid_rows and not target.allow_empty:
        raise conflict("plan_empty", "该查询需要实际观察到供给")
    return pages


def assign(db, plan_id: str, slot: str, body: Assignment) -> dict:
    plan = load(db, plan_id)
    target = next((row for row in plan.targets if row.slot == slot), None)
    if target is None:
        raise missing("slot", slot)
    latest = head(db, plan_id, slot)
    if latest is not None and latest["session_id"] == body.session_id:
        return dict(latest)
    current = 0 if latest is None else latest["generation"]
    if current != body.expected_generation:
        raise conflict("assignment_changed", "目标已由其他操作员更换采集会话",
                       {"generation": current})
    validate_selection(db, target, body.session_id)
    previous = db.execute(
        "SELECT slot FROM assignments WHERE plan_id=? AND session_id=? LIMIT 1",
        (plan_id, body.session_id),
    ).fetchone()
    if previous is not None and previous["slot"] != slot:
        raise conflict("session_reused", "同一次采集不能计作两个查询的覆盖")
    db.execute("INSERT INTO assignments VALUES(?,?,?,?,?)",
               (plan_id, slot, current + 1, body.session_id, clock()))
    return {"session_id": body.session_id, "generation": current + 1}


def read(db, plan_id: str) -> dict:
    data = load(db, plan_id).model_dump(mode="json")
    active = {}
    for target in data["targets"]:
        result = head(db, plan_id, target["slot"])
        if result is not None:
            active[target["slot"]] = dict(result)
    return {**data, "active_assignments": active}


def assignment_history(db, plan_id: str, slot: str) -> list[dict]:
    plan = load(db, plan_id)
    if not any(target.slot == slot for target in plan.targets):
        raise missing("slot", slot)
    rows = db.execute(
        """SELECT generation,session_id,assigned_at FROM assignments WHERE plan_id=? AND slot=?
           ORDER BY generation""", (plan_id, slot),
    )
    return [dict(row) for row in rows]


def evaluate_target(db, plan: NewPlan, target: Target, at: datetime) -> dict:
    selected = head(db, plan.plan_id, target.slot)
    report = {"target": target.model_dump(mode="json"), "state": "missing", "session_id": None,
              "generation": 0, "pages": []}
    if selected is None:
        return report
    try:
        pages = validate_selection(db, target, selected["session_id"])
    except DomainError as exc:
        return {**report, **dict(selected), "state": "unusable", "reason": exc.code}
    oldest = datetime.fromisoformat(min(page["captured_at"] for page in pages))
    last = datetime.fromisoformat(max(page["captured_at"] for page in pages))
    if last > at:
        state = "future_capture"
    elif at - oldest > timedelta(hours=plan.max_capture_age_hours):
        state = "stale_capture"
    else:
        state = "covered"
    return {
        **report, **dict(selected), "state": state,
        "pages": [
            {key: page[key] for key in ("page_no", "page_digest", "captured_at", "html_digest")}
            for page in pages
        ],
    }


def readiness(db, plan_id: str, at: datetime) -> dict:
    if at.tzinfo is None:
        raise invalid("evaluation_time", "评估时间必须包含时区")
    plan = load(db, plan_id)
    statuses = [evaluate_target(db, plan, target, at) for target in plan.targets]
    gaps = [
        {"slot": row["target"]["slot"], "reason": row["state"]}
        for row in statuses
        if row["target"]["necessity"] == "required" and row["state"] != "covered"
    ]
    return {
        "plan_id": plan_id, "region": plan.region,
        "as_of": at.astimezone(timezone.utc).isoformat(),
        "targets": statuses, "gaps": gaps, "ready": not gaps,
    }


def included_sessions(report: dict) -> set[str]:
    return {row["session_id"] for row in report["targets"] if row["state"] == "covered"}

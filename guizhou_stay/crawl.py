from __future__ import annotations

from datetime import datetime, timezone
import json
from sqlite3 import Connection

from guizhou_stay.contracts import Page, SessionOpen
from guizhou_stay.digest import digest
from guizhou_stay.errors import conflict, invalid, missing
from guizhou_stay.storage import dump


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def open_session(db: Connection, body: SessionOpen) -> dict:
    stamp = now()
    row = db.execute("SELECT * FROM sessions WHERE session_id=?", (body.session_id,)).fetchone()
    if row:
        fields = ("source", "query", "source_revision", "expected_pages", "locale")
        if any(row[key] != getattr(body, key) for key in fields):
            raise conflict("session_changed", "采集会话编号已对应另一份查询")
        return dict(row)
    db.execute(
        """INSERT INTO sessions(session_id,source,query,source_revision,expected_pages,
           locale,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
        (body.session_id, body.source, body.query, body.source_revision,
         body.expected_pages, body.locale, "open", stamp, stamp),
    )
    return dict(session(db, body.session_id))


def session(db: Connection, session_id: str):
    row = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if row is None:
        raise missing("session", session_id)
    return row


def scope_delta(current, body: Page) -> dict:
    fields = {
        "source": body.source,
        "query": body.query,
        "source_revision": body.source_revision,
        "expected_pages": body.total_pages,
        "locale": body.locale,
    }
    return {
        field: {"expected": current[field], "observed": value}
        for field, value in fields.items()
        if current[field] != value
    }


def previous_page(db: Connection, session_id: str):
    return db.execute(
        """SELECT page_no,next_token,captured_at FROM pages
           WHERE session_id=? ORDER BY page_no DESC LIMIT 1""", (session_id,)
    ).fetchone()


def validate_continuation(db: Connection, body: Page):
    previous = previous_page(db, body.session_id)
    expected = previous["page_no"] + 1 if previous is not None else 1
    if body.page_no != expected:
        raise conflict("page_gap", "页面必须按连续页码提交", {"expected_page": expected})
    token = previous["next_token"] if previous is not None else None
    if body.request_token != token:
        raise conflict("cursor_mismatch", "请求游标与前一页提供的游标不一致",
                       {"expected_token": token})
    if previous is not None and body.captured_at.isoformat() < previous["captured_at"]:
        raise invalid("capture_order", "采集时间不能早于前一页")
    if body.next_token and db.execute(
        "SELECT 1 FROM pages WHERE session_id=? AND next_token=?",
        (body.session_id, body.next_token),
    ).fetchone():
        raise conflict("cursor_loop", "下一页游标指向同次采集已使用的页面")


def save_page(db: Connection, body: Page) -> dict:
    current = session(db, body.session_id)
    payload = body.model_dump(mode="json")
    page_digest = digest(payload)
    delta = scope_delta(current, body)
    if delta and current["state"] == "open":
        db.execute("UPDATE sessions SET state='stale', updated_at=?, reason=? WHERE session_id=?",
                   (now(), dump(delta), body.session_id))
        return {"stale": delta}
    old = db.execute(
        "SELECT page_digest FROM pages WHERE session_id=? AND page_no=?",
        (body.session_id, body.page_no),
    ).fetchone()
    if old:
        if old["page_digest"] != page_digest:
            raise conflict("page_changed", "同一页码返回了不同内容")
        return {"page_no": body.page_no, "replayed": True, "page_digest": page_digest,
                "complete": current["state"] == "complete"}
    if current["state"] != "open":
        raise conflict("session_closed", "采集会话已结束，不能追加页面")
    validate_continuation(db, body)
    db.execute(
        "INSERT INTO pages VALUES(?,?,?,?,?,?,?,?)",
        (body.session_id, body.page_no, page_digest, body.source_revision, body.captured_at.isoformat(),
         body.viewport, dump(payload), body.next_token),
    )
    complete = body.page_no == current["expected_pages"]
    state = "complete" if complete else "open"
    db.execute("UPDATE sessions SET state=?,updated_at=? WHERE session_id=?",
               (state, now(), body.session_id))
    return {"page_no": body.page_no, "replayed": False, "page_digest": page_digest,
            "complete": complete}


def read_pages(db: Connection, session_id: str, *, revision: int | None = None) -> list[dict]:
    current = session(db, session_id)
    if revision is not None and revision != current["source_revision"]:
        raise invalid("revision_mismatch", "请求修订与采集会话不一致")
    rows = db.execute("SELECT payload FROM pages WHERE session_id=? ORDER BY page_no", (session_id,))
    return [json.loads(row["payload"]) for row in rows]


def session_report(db: Connection, session_id: str) -> dict:
    row = session(db, session_id)
    pages = db.execute(
        """SELECT page_no,page_digest,source_revision,captured_at,next_token FROM pages
           WHERE session_id=? ORDER BY page_no""", (session_id,)
    ).fetchall()
    return {
        "session_id": row["session_id"], "source": row["source"], "query": row["query"],
        "source_revision": row["source_revision"], "state": row["state"], "reason": row["reason"],
        "locale": row["locale"], "expected_pages": row["expected_pages"],
        "pages": [dict(page) for page in pages], "received_pages": len(pages),
        "complete": row["state"] == "complete",
    }


def abort_session(db: Connection, session_id: str, reason: str) -> dict:
    row = session(db, session_id)
    if row["state"] == "complete":
        raise conflict("session_complete", "完整会话不能标记失败")
    db.execute("UPDATE sessions SET state='failed',updated_at=?,reason=? WHERE session_id=?",
               (now(), reason, session_id))
    return {"session_id": session_id, "state": "failed", "reason": reason}

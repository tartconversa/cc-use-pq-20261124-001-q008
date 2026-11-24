from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Callable
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from guizhou_stay import catalog, crawl
from guizhou_stay.contracts import Key
from guizhou_stay.digest import digest_text
from guizhou_stay.errors import DomainError, conflict, invalid, missing
from guizhou_stay.portal import Capture, CaptureFailure, Reader, convert, session_from_first
from guizhou_stay.storage import Store, dump

MAX_ATTEMPTS = 4
LEASE_SECONDS = 60


def utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


@dataclass(frozen=True)
class Claim:
    session_id: str
    page: int
    cursor: str | None
    generation: str
    attempt: int
    expires_at: datetime


def record_event(db, session_id, page, code, detail, *, at):
    db.execute(
        """INSERT INTO capture_events(session_id,page_no,code,detail,observed_at)
           VALUES(?,?,?,?,?)""",
        (session_id, page, code, dump(detail), at.isoformat()),
    )


def check_collection(db, session_id):
    row = db.execute("SELECT * FROM collections WHERE session_id=?", (session_id,)).fetchone()
    if row is None:
        raise missing("collection", session_id)
    return row


def register(store: Store, session_id: str, source_label: str, reader: Reader) -> dict:
    with store.transaction() as db:
        existing = db.execute("SELECT * FROM collections WHERE session_id=?", (session_id,)).fetchone()
        if existing is not None:
            if existing["source_label"] != source_label:
                raise conflict("reader_changed", "恢复时需使用原回放来源")
            return progress(db, session_id)
    html, url = reader.read(None)
    capture = convert(session_id, html, url)
    opened = session_from_first(session_id, capture)
    with store.transaction(write=True) as db:
        existing = db.execute("SELECT * FROM collections WHERE session_id=?", (session_id,)).fetchone()
        if existing is None:
            if db.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)).fetchone():
                raise conflict("manual_session", "此编号已用于人工提交")
            crawl.open_session(db, opened)
            db.execute("INSERT INTO collections(session_id,source_label) VALUES (?,?)",
                       (session_id, source_label))
            return commit_page(db, capture, at=utc())
        if existing["source_label"] != source_label:
            raise conflict("reader_changed", "恢复时需使用原回放来源")
        return progress(db, session_id)


def claim_next(store: Store, session_id: str, at: datetime) -> Claim | None:
    with store.transaction(write=True) as db:
        row = check_collection(db, session_id)
        current = crawl.session(db, session_id)
        if current["state"] != "open":
            return None
        if row["retry_at"] and at < datetime.fromisoformat(row["retry_at"]):
            return None
        expired = row["lease_expires"] and at >= datetime.fromisoformat(row["lease_expires"])
        if row["generation"] and not expired:
            return None
        if row["attempts"] >= MAX_ATTEMPTS:
            finish_failure(db, session_id, row["next_page"], "attempt_limit",
                           "当前页已用完采集次数", at=at)
            return None
        generation = uuid4().hex
        expiry = at + timedelta(seconds=LEASE_SECONDS)
        claim = Claim(session_id, row["next_page"], row["cursor"], generation,
                      row["attempts"] + 1, expiry)
        db.execute(
            """UPDATE collections SET generation=?, lease_expires=?,attempts=?,retry_at=NULL
               WHERE session_id=?""",
            (generation, expiry.isoformat(), claim.attempt, session_id),
        )
        record_event(db, session_id, claim.page, "claimed",
                     {"attempt": claim.attempt, "recovered": bool(expired)}, at=at)
        return claim


def check_claim(db, claim: Claim, at: datetime):
    current = crawl.session(db, claim.session_id)
    if current["state"] != "open":
        raise conflict("session_closed", "采集会话已结束，租用的工作不能入库")
    row = check_collection(db, claim.session_id)
    if row["generation"] != claim.generation or row["next_page"] != claim.page:
        raise conflict("lease_replaced", "该采集结果已失去提交资格")
    if at >= claim.expires_at:
        raise conflict("lease_expired", "采集租期已过，请重新领取")


def finish_failure(db, session_id, page, code, message, *, at):
    crawl.abort_session(db, session_id, message)
    db.execute(
        "UPDATE collections SET generation=NULL,lease_expires=NULL,retry_at=NULL WHERE session_id=?",
        (session_id,),
    )
    record_event(db, session_id, page, code, {"state": "failed", "message": message}, at=at)


def fail_claim(store: Store, claim: Claim, failure: DomainError, *, at: datetime):
    with store.transaction(write=True) as db:
        check_claim(db, claim, at)
        if getattr(failure, "retryable", False) and claim.attempt < MAX_ATTEMPTS:
            wait = timedelta(seconds=15 * 2 ** (claim.attempt - 1))
            next_at = at + wait
            db.execute(
                """UPDATE collections SET generation=NULL,lease_expires=NULL,retry_at=?
                   WHERE session_id=?""", (next_at.isoformat(), claim.session_id),
            )
            record_event(db, claim.session_id, claim.page, failure.code,
                         {"state": "waiting", "retry_at": next_at.isoformat(),
                          "details": failure.details}, at=at)
        else:
            finish_failure(db, claim.session_id, claim.page, failure.code, failure.message, at=at)
        return progress(db, claim.session_id)


def commit_page(db, capture: Capture, *, at: datetime, claim: Claim | None = None):
    page = capture.page
    if claim is not None:
        check_claim(db, claim, at)
        if page.page_no != claim.page or page.request_token != claim.cursor:
            raise conflict("cursor_response", "合作方返回的页码或回显游标与所领取页面不一致")
    receipt = crawl.save_page(db, page)
    if receipt.get("stale"):
        db.execute("UPDATE collections SET generation=NULL,lease_expires=NULL WHERE session_id=?",
                   (page.session_id,))
        record_event(db, page.session_id, page.page_no, "scope_changed", receipt["stale"], at=at)
        return progress(db, page.session_id)
    db.execute(
        """INSERT INTO captures(session_id,page_no,locator,raw_html,html_digest)
           VALUES(?,?,?,?,?)""",
        (page.session_id, page.page_no, capture.url, capture.html, digest_text(capture.html)),
    )
    catalog.ingest_page(db, page)
    db.execute(
        """UPDATE collections SET next_page=?,cursor=?,attempts=0,
           generation=NULL,lease_expires=NULL,retry_at=NULL WHERE session_id=?""",
        (page.page_no + 1, page.next_token, page.session_id),
    )
    record_event(db, page.session_id, page.page_no, "captured", capture.receipt(), at=at)
    return progress(db, page.session_id)


def step(store: Store, session_id: str, reader: Reader, *,
         clock: Callable[[], datetime] = utc) -> dict:
    claim = claim_next(store, session_id, clock())
    if claim is None:
        with store.transaction() as db:
            return progress(db, session_id)
    try:
        html, url = reader.read(claim.cursor)
        capture = convert(session_id, html, url)
        with store.transaction(write=True) as db:
            return commit_page(db, capture, claim=claim, at=clock())
    except DomainError as failure:
        if failure.code in {"lease_replaced", "lease_expired", "session_closed"}:
            raise
        return fail_claim(store, claim, failure, at=clock())


def run(store: Store, session_id: str, source_label: str, reader: Reader, limit: int) -> dict:
    try:
        TypeAdapter(Key).validate_python(session_id)
    except ValidationError as exc:
        raise invalid("session_id", "采集会话编号不合法") from exc
    if not 1 <= limit <= 100:
        raise invalid("capture_limit", "采集页预算应在 1 到 100 之间")
    try:
        existing = read_progress(store, session_id)
    except DomainError as exc:
        if exc.code != "collection_missing":
            raise
        existing = None
    previous = register(store, session_id, source_label, reader)
    budget = limit if existing is not None else limit - 1
    for _ in range(budget):
        if previous["state"] != "open":
            break
        current = step(store, session_id, reader)
        if current["received_pages"] == previous["received_pages"]:
            return current
        previous = current
    return previous


def read_progress(store: Store, session_id):
    with store.transaction() as db:
        return progress(db, session_id)


def progress(db, session_id):
    lease = check_collection(db, session_id)
    report = crawl.session_report(db, session_id)
    records = db.execute(
        """SELECT page_no,code,detail,observed_at FROM capture_events
           WHERE session_id=? ORDER BY seq DESC LIMIT 30""", (session_id,)
    ).fetchall()
    report.update(
        next_page=lease["next_page"] if report["state"] == "open" else None,
        next_token=lease["cursor"] if report["state"] == "open" else None,
        attempts=lease["attempts"],
        retry_at=lease["retry_at"],
        events=[{**dict(row), "detail": json.loads(row["detail"])} for row in reversed(records)],
    )
    return report


def raw_page(db, session_id: str, page_no: int) -> dict:
    row = db.execute(
        "SELECT locator,raw_html,html_digest FROM captures WHERE session_id=? AND page_no=?",
        (session_id, page_no),
    ).fetchone()
    if row is None:
        raise missing("capture", f"{session_id}/{page_no}")
    return dict(row)

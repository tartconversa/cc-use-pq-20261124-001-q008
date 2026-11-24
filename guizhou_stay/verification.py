from __future__ import annotations

import json

from guizhou_stay.digest import digest, digest_text
from guizhou_stay.errors import missing
from guizhou_stay.portal import convert
from guizhou_stay.errors import DomainError


def issue(code: str, **details) -> dict:
    return {"code": code, **details}


def verified_pages(db, session_id: str):
    return db.execute(
        """SELECT p.page_no,p.payload,p.page_digest,c.raw_html,c.html_digest,c.locator
           FROM pages AS p LEFT JOIN captures AS c USING(session_id,page_no)
           WHERE p.session_id=? ORDER BY p.page_no""", (session_id,),
    ).fetchall()


def replay_page(session_id: str, row) -> dict:
    page_no = row["page_no"]
    issues = []
    document = json.loads(row["payload"])
    if digest(document) != row["page_digest"]:
        issues.append(issue("structured_digest", page=page_no))
    has_raw = row["raw_html"] is not None
    if has_raw:
        if digest_text(row["raw_html"]) != row["html_digest"]:
            issues.append(issue("html_digest", page=page_no))
        try:
            parsed = convert(session_id, row["raw_html"], row["locator"]).page
        except DomainError as exc:
            issues.append(issue("html_replay", page=page_no, reason=exc.code))
        else:
            parsed_document = parsed.model_dump(mode="json")
            if parsed_document != document:
                different = sorted(key for key in parsed_document
                                   if parsed_document[key] != document.get(key))
                issues.append(issue("extraction_drift", page=page_no, fields=different))
    return {
        "page": page_no, "items": len(document["rows"]), "has_raw": has_raw,
        "page_digest": row["page_digest"], "issues": issues, "document": document,
    }


def audit_continuation(pages: list[dict], session) -> list[dict]:
    expected_cursor = None
    expected_pages = session["expected_pages"]
    findings = []
    used = set()
    last_capture = ""
    for number, row in enumerate(pages, 1):
        document = row["document"]
        if document["page_no"] != number or row["page"] != number:
            findings.append(issue("page_sequence", expected=number, observed=row["page"]))
        if document["request_token"] != expected_cursor:
            findings.append(issue("cursor_sequence", page=number))
        if document["total_pages"] != expected_pages:
            findings.append(issue("page_total", page=number))
        if document["source"] != session["source"] or document["query"] != session["query"]:
            findings.append(issue("capture_scope", page=number))
        if document["source_revision"] != session["source_revision"]:
            findings.append(issue("capture_revision", page=number))
        if document["locale"] != session["locale"]:
            findings.append(issue("capture_locale", page=number))
        captured = document["captured_at"]
        if captured < last_capture:
            findings.append(issue("capture_time", page=number))
        last_capture = captured
        expected_cursor = document["next_token"]
        if expected_cursor is not None:
            if expected_cursor in used:
                findings.append(issue("cursor_loop", page=number, cursor=expected_cursor))
            used.add(expected_cursor)
    if session["state"] == "complete" and expected_cursor is not None:
        findings.append(issue("terminal_cursor"))
    return findings


def observation_groups(db, session_id: str) -> dict:
    rows = db.execute(
        """SELECT o.item_id,o.page_no,s.digest FROM observations AS o
           JOIN supplies AS s USING(item_id,source,source_revision)
           WHERE o.session_id=? ORDER BY o.page_no,o.item_id""", (session_id,)
    )
    return {(row["page_no"], row["item_id"]): row["digest"] for row in rows}


def expected_observations(pages: list[dict]) -> dict:
    from guizhou_stay.catalog import gather_rows
    from guizhou_stay.contracts import Page

    result = {}
    for row in pages:
        page = Page.model_validate(row["document"])
        for item, fingerprint in gather_rows(page):
            result[row["page"], item.item_id] = fingerprint
    return result


def audit_observations(db, session, pages):
    observed = observation_groups(db, session["session_id"])
    if session["state"] != "complete":
        return [issue("partially_promoted")] if observed else []
    if len(pages) != session["expected_pages"]:
        return [issue("missing_pages", expected=session["expected_pages"], observed=len(pages))]
    expected = expected_observations(pages)
    findings = []
    for key in sorted(expected.keys() | observed.keys()):
        if expected.get(key) != observed.get(key):
            page, item_id = key
            findings.append(issue("observation_differs", page=page, item_id=item_id))
    return findings


def review(db, session_id: str) -> dict:
    session = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if session is None:
        raise missing("session", session_id)
    pages = [replay_page(session_id, row) for row in verified_pages(db, session_id)]
    findings = [problem for page in pages for problem in page["issues"]]
    findings.extend(audit_continuation(pages, session))
    if not findings:
        try:
            findings.extend(audit_observations(db, session, pages))
        except DomainError as exc:
            findings.append(issue("invalid_observation", reason=exc.code))
    return {
        "session_id": session_id, "state": session["state"], "valid": not findings,
        "pages": [{k: v for k, v in page.items() if k != "document"} for page in pages],
        "findings": findings,
    }

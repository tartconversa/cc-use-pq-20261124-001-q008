from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
 session_id TEXT PRIMARY KEY, source TEXT NOT NULL, query TEXT NOT NULL,
 source_revision INTEGER NOT NULL, expected_pages INTEGER NOT NULL, locale TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('open','complete','stale','failed')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, reason TEXT
);
CREATE TABLE IF NOT EXISTS pages(
 session_id TEXT NOT NULL REFERENCES sessions(session_id), page_no INTEGER NOT NULL,
 page_digest TEXT NOT NULL, source_revision INTEGER NOT NULL, captured_at TEXT NOT NULL,
 viewport TEXT NOT NULL, payload TEXT NOT NULL, next_token TEXT,
 PRIMARY KEY(session_id,page_no)
);
CREATE TABLE IF NOT EXISTS collections(
 session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
 source_label TEXT NOT NULL, cursor TEXT, next_page INTEGER NOT NULL DEFAULT 1,
 attempts INTEGER NOT NULL DEFAULT 0, generation TEXT, lease_expires TEXT,retry_at TEXT
);
CREATE TABLE IF NOT EXISTS captures(
 session_id TEXT NOT NULL, page_no INTEGER NOT NULL,
 locator TEXT NOT NULL, raw_html TEXT NOT NULL, html_digest TEXT NOT NULL,
 PRIMARY KEY(session_id,page_no),
 FOREIGN KEY(session_id,page_no) REFERENCES pages(session_id,page_no)
);
CREATE TABLE IF NOT EXISTS capture_events(
 seq INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES collections(session_id),
 page_no INTEGER NOT NULL, code TEXT NOT NULL, detail TEXT NOT NULL, observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staged(
 session_id TEXT NOT NULL, page_no INTEGER NOT NULL, item_id TEXT NOT NULL,
 digest TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(session_id,page_no,item_id),
 FOREIGN KEY(session_id,page_no) REFERENCES pages(session_id,page_no)
);
CREATE TABLE IF NOT EXISTS observations(
 item_id TEXT NOT NULL, source TEXT NOT NULL, source_revision INTEGER NOT NULL,
 session_id TEXT NOT NULL, page_no INTEGER NOT NULL,
 PRIMARY KEY(item_id,source,source_revision,session_id,page_no),
 FOREIGN KEY(session_id,page_no) REFERENCES pages(session_id,page_no)
);
CREATE TABLE IF NOT EXISTS supplies(
 item_id TEXT NOT NULL, source TEXT NOT NULL, source_revision INTEGER NOT NULL,
 digest TEXT NOT NULL, payload TEXT NOT NULL, received_at TEXT NOT NULL,
 PRIMARY KEY(item_id,source,source_revision)
);
CREATE TABLE IF NOT EXISTS identities(
 identity_id TEXT PRIMARY KEY, kind TEXT NOT NULL, canonical TEXT NOT NULL,
 aliases TEXT NOT NULL, current_digest TEXT NOT NULL, status TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identity_sources(
 identity_id TEXT NOT NULL REFERENCES identities(identity_id), item_id TEXT NOT NULL,
 source TEXT NOT NULL, source_revision INTEGER NOT NULL, digest TEXT NOT NULL,
 PRIMARY KEY(identity_id,item_id,source,source_revision)
);
CREATE TABLE IF NOT EXISTS evidence(
 evidence_id TEXT PRIMARY KEY, supply_id TEXT NOT NULL, category TEXT NOT NULL,
 issuer TEXT NOT NULL, issued_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 source TEXT NOT NULL, source_revision INTEGER NOT NULL, document_ref TEXT NOT NULL,
 status TEXT NOT NULL, digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans(
 plan_id TEXT PRIMARY KEY,digest TEXT NOT NULL,document TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments(
 plan_id TEXT NOT NULL REFERENCES plans(plan_id), slot TEXT NOT NULL,generation INTEGER NOT NULL,
 session_id TEXT NOT NULL REFERENCES sessions(session_id),assigned_at TEXT NOT NULL,
 PRIMARY KEY(plan_id,slot,generation)
);
CREATE TABLE IF NOT EXISTS packages(
 package_id TEXT NOT NULL, revision INTEGER NOT NULL, region TEXT NOT NULL,
 policy TEXT NOT NULL, items TEXT NOT NULL, gaps TEXT NOT NULL,
 digest TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('draft','published','superseded')),
 created_at TEXT NOT NULL, request_digest TEXT NOT NULL,document TEXT NOT NULL,
 PRIMARY KEY(package_id,revision), UNIQUE(package_id,request_digest)
);
CREATE TABLE IF NOT EXISTS deliveries(
 destination TEXT NOT NULL, delivery_id TEXT NOT NULL,package_id TEXT NOT NULL,edition INTEGER NOT NULL,
 sequence INTEGER NOT NULL,package_digest TEXT NOT NULL,request_digest TEXT NOT NULL,
 queued_at TEXT NOT NULL,acknowledged_at TEXT,receiver_reference TEXT,
 PRIMARY KEY(destination,delivery_id),UNIQUE(destination,sequence),UNIQUE(destination,receiver_reference),
 FOREIGN KEY(package_id,edition) REFERENCES packages(package_id,revision)
);
CREATE TABLE IF NOT EXISTS audit(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, object_id TEXT NOT NULL,
 before_digest TEXT, after_digest TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS page_revision ON pages(session_id,source_revision);
CREATE INDEX IF NOT EXISTS supply_region ON supplies(source_revision);
CREATE INDEX IF NOT EXISTS staged_duplicates ON staged(session_id,item_id,page_no);
CREATE INDEX IF NOT EXISTS proof_version ON evidence(source,supply_id,category,source_revision);
CREATE INDEX IF NOT EXISTS collection_history ON capture_events(session_id,seq);
"""


class Store:
    def __init__(self, location: str | Path):
        self.location = Path(location)
        self.location.parent.mkdir(parents=True, exist_ok=True)
        db = self.connect()
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(SCHEMA)
        finally:
            db.close()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.location, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def transaction(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


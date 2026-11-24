from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from guizhou_stay.api import create_app


def stamp(days=0):
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


def page(page_no, rows, *, session="hk-001", revision=4, total=2, has_next=None, token=None):
    if has_next is None:
        has_next = page_no < total
    return {"session_id": session, "source": "partner-map", "query": "贵州旅居",
            "page_no": page_no, "total_pages": total, "source_revision": revision,
            "captured_at": stamp(), "request_token": f"p{page_no}" if page_no > 1 else None, "viewport": f"desktop-{page_no}", "rows": rows,
            "has_next": has_next, "next_token": token or (f"p{page_no+1}" if has_next else None)}


def item(item_id, title, category="residence", *, provider="partner-a", region="贵阳", status="active", days=2):
    return {"item_id": item_id, "kind": "property" if category == "residence" else "service",
            "title": title, "provider_id": provider, "city": region, "region": region,
            "category": category, "status": status, "updated_at": stamp(days),
            "source_revision": 4, "address_hint": "合成地址", "price_band": "中",
            "service_window": "09:00-18:00", "tags": ["旅居", category]}


def evidence(evidence_id, supply_id, category, *, source_revision=4, days=2, expires=30, status="verified"):
    issued = datetime.now(timezone.utc) - timedelta(days=days)
    return {"evidence_id": evidence_id, "supply_id": supply_id, "category": category,
            "issuer": "合成服务中心", "issued_at": issued.replace(microsecond=0).isoformat(),
            "expires_at": (issued + timedelta(days=expires)).replace(microsecond=0).isoformat(),
            "source": "partner-map", "source_revision": source_revision,
            "document_ref": f"doc-{evidence_id}", "status": status}


def policy(categories=("residence", "medical", "food", "language"), minimum=1, age=90):
    return {"required_categories": list(categories), "minimum_active_items": minimum,
            "max_source_age_days": age, "require_verified_evidence": True, "allow_pending": False}


class ApiCase(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = TestClient(create_app(Path(self.temp.name) / "stay.sqlite3"))
        self.addCleanup(self.client.close)

    def call(self, method, path, data=None, *, status=200, params=None):
        response = self.client.request(method, path, json=data, params=params)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def open(self, session="hk-001", revision=4, pages=2):
        return self.call("POST", "/sessions", {"session_id": session, "source": "partner-map",
            "query": "贵州旅居", "source_revision": revision, "expected_pages": pages})

    def send_page(self, data, *, session="hk-001", status=200):
        return self.call("POST", f"/sessions/{session}/pages", data, status=status)

    def add_evidence(self, data, status=200):
        return self.call("POST", "/evidence", data, status=status)


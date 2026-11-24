from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

import httpx

from guizhou_stay import collector, verification
from guizhou_stay.errors import DomainError
from guizhou_stay.portal import CaptureFailure, FolderReader, LoopbackReader, convert
from guizhou_stay.storage import Store

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "partner"
AS_OF = "2026-11-21T01:00:00+00:00"


class CaptureTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.pages = self.root / "portal"
        shutil.copytree(FIXTURES, self.pages)
        self.store = Store(self.root / "catalog.sqlite3")
        self.reader = FolderReader(self.pages)
        self.session_id = "capture-one"
        self.current = datetime(2026, 11, 21, tzinfo=timezone.utc)

    def opened(self):
        return collector.register(self.store, self.session_id, self.pages.as_uri(), self.reader)

    def run_next(self):
        return collector.step(self.store, self.session_id, self.reader, clock=lambda: self.current)

    def instances(self):
        with self.store.transaction() as db:
            return db.execute("SELECT item_id FROM supplies ORDER BY item_id").fetchall()

    def test_first_page_is_checkpointed_without_publishing_partial_supply(self):
        first = self.opened()
        self.assertEqual((first["state"], first["next_page"], first["next_token"]), ("open", 2, "p2"))
        self.assertEqual(len(self.instances()), 0)
        second = self.run_next()
        self.assertTrue(second["complete"])
        self.assertEqual([row["item_id"] for row in self.instances()],
                         ["food-1", "lang-1", "med-1", "stay-1"])
        with self.store.transaction() as db:
            audit = verification.review(db, self.session_id)
            proof = collector.raw_page(db, self.session_id, 2)
        self.assertTrue(audit["valid"])
        self.assertEqual([row["has_raw"] for row in audit["pages"]], [True, True])
        self.assertIn("長者餐廳", proof["raw_html"])

    def test_restart_uses_existing_cursor_without_redownloading_first_page(self):
        self.opened()
        (self.pages / "first.html").unlink()
        restarted = Store(self.root / "catalog.sqlite3")
        report = collector.run(restarted, self.session_id, self.pages.as_uri(), self.reader, limit=1)
        self.assertTrue(report["complete"])
        achieved = collector.run(restarted, self.session_id, self.pages.as_uri(), self.reader, limit=1)
        self.assertEqual(report, achieved)

    def test_different_source_path_cannot_be_used_on_resume(self):
        self.opened()
        with self.assertRaises(DomainError) as result:
            collector.run(self.store, self.session_id, "other", self.reader, limit=1)
        self.assertEqual(result.exception.code, "reader_changed")
        self.assertEqual(collector.read_progress(self.store, self.session_id)["next_token"], "p2")

    def test_portal_revision_change_stops_without_materializing_first_page(self):
        self.opened()
        second = self.pages / "p2.html"
        html = second.read_text(encoding="utf-8")
        second.write_text(html.replace('stay-revision" content="4', 'stay-revision" content="5'),
                          encoding="utf-8")
        report = self.run_next()
        self.assertEqual(report["state"], "stale")
        self.assertEqual(report["received_pages"], 1)
        self.assertEqual(report["events"][-1]["code"], "scope_changed")
        self.assertEqual(self.instances(), [])

    def test_response_to_wrong_cursor_does_not_commit_page(self):
        self.opened()
        second = self.pages / "p2.html"
        html = second.read_text(encoding="utf-8").replace('stay-cursor" content="p2', 'stay-cursor" content="previous')
        second.write_text(html, encoding="utf-8")
        report = self.run_next()
        self.assertEqual(report["state"], "failed")
        self.assertEqual(report["events"][-1]["code"], "cursor_response")
        self.assertEqual(len(report["pages"]), 1)

    def test_temporary_missing_page_backoff_then_resume(self):
        self.opened()
        second = self.pages / "p2.html"
        original = second.read_bytes()
        second.unlink()
        waiting = self.run_next()
        self.assertEqual(waiting["state"], "open")
        self.assertEqual(waiting["attempts"], 1)
        self.assertEqual(datetime.fromisoformat(waiting["retry_at"]) - self.current, timedelta(seconds=15))
        second.write_bytes(original)
        self.assertEqual(self.run_next(), waiting)
        self.current += timedelta(seconds=15)
        done = self.run_next()
        self.assertEqual(done["state"], "complete")
        self.assertIsNone(done["retry_at"])

    def test_old_worker_cannot_commit_after_lease_replacement(self):
        self.opened()
        claim = collector.claim_next(self.store, self.session_id, self.current)
        self.assertEqual(claim.attempt, 1)
        self.assertIsNone(collector.claim_next(self.store, self.session_id, self.current))
        self.current += timedelta(seconds=60)
        recovered = collector.claim_next(self.store, self.session_id, self.current)
        html, url = self.reader.read("p2")
        capture = convert(self.session_id, html, url)
        with self.assertRaises(DomainError) as replaced, self.store.transaction(write=True) as db:
            collector.commit_page(db, capture, claim=claim, at=self.current)
        self.assertEqual(replaced.exception.code, "lease_replaced")
        with self.store.transaction(write=True) as db:
            completed = collector.commit_page(db, capture, claim=recovered, at=self.current)
        self.assertTrue(completed["complete"])
        self.assertEqual(sum(item["code"] == "captured" for item in completed["events"]), 2)

    def test_audit_detects_raw_html_and_extraction_drift_independently(self):
        self.opened()
        self.run_next()
        with self.store.transaction(write=True) as db:
            db.execute(
                """UPDATE captures SET raw_html=REPLACE(raw_html,'長者餐廳','已更改')
                   WHERE session_id=? AND page_no=2""", (self.session_id,),
            )
        with self.store.transaction() as db:
            report = verification.review(db, self.session_id)
        self.assertFalse(report["valid"])
        self.assertEqual({finding["code"] for finding in report["findings"]},
                         {"html_digest", "extraction_drift"})

    def test_page_with_incomplete_visible_card_does_not_advance(self):
        html, url = self.reader.read(None)
        changed = html.replace('data-id="med-1"', "")
        with self.assertRaises(CaptureFailure) as failure:
            convert(self.session_id, changed, url)
        self.assertEqual(failure.exception.details["positions"], [2])
        valid = convert(self.session_id, html, url)
        self.assertEqual([row["item_id"] for row in valid.page.rows], ["stay-1", "med-1"])

    def test_empty_page_requires_explicit_portal_confirmation(self):
        html, url = self.reader.read(None)
        header = html.split("<body>", 1)[0]
        with self.assertRaises(CaptureFailure) as failure:
            convert(self.session_id, header + "<body></body></html>", url)
        self.assertEqual(failure.exception.code, "unconfirmed_empty")
        explicit = header.replace("</head>", '<meta name="stay-empty" content="confirmed"></head>')
        captured = convert(self.session_id, explicit + "<body></body></html>", url)
        self.assertEqual(captured.page.rows, [])

    def test_reader_does_not_follow_redirect_or_use_remote_address(self):
        called = []

        def redirect(request):
            called.append(str(request.url))
            return httpx.Response(302, headers={"location": "https://example.test/"})

        with httpx.Client(transport=httpx.MockTransport(redirect)) as client:
            with self.assertRaises(DomainError) as nonlocal_address:
                LoopbackReader("http://example.test/offers", client)
            self.assertEqual(nonlocal_address.exception.code, "portal_origin")
            reader = LoopbackReader("http://127.0.0.1:8282/offers", client)
            with self.assertRaises(CaptureFailure) as no_redirect:
                reader.read("p2")
        self.assertEqual(no_redirect.exception.details["status"], 302)
        self.assertEqual(called, ["http://127.0.0.1:8282/offers?cursor=p2"])

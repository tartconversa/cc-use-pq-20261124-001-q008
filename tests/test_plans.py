from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
from zipfile import ZipFile

from guizhou_stay import bundles, collector
from guizhou_stay.digest import digest
from guizhou_stay.portal import FolderReader
from guizhou_stay.storage import Store
from tests.support import ApiCase, evidence, item, page, policy, stamp
from tests.test_capture import AS_OF, FIXTURES


class PlanTest(ApiCase):
    def setUp(self):
        super().setUp()
        self.at = stamp()
        self.catalog = "/coverage/preview"
        self.package_id = "alpine-guide"
        self.target = {
            "slot": "directory",
            "source": "partner-map",
            "query": "贵州旅居",
            "source_revision": 4,
        }

    def plan(self, targets=None, *, status=200):
        return self.call("POST", "/plans", {
            "plan_id": "hk-catalog", "region": "贵阳", "targets": targets or [self.target],
        }, status=status)

    def assign(self, session, expected, *, slot="directory", status=200):
        return self.call("PUT", f"/plans/hk-catalog/slots/{slot}", {
            "session_id": session, "expected_generation": expected,
        }, status=status)

    def structured(self, session="hk-001", rows=()):
        self.open(session=session, pages=1)
        values = list(rows) or [item("residence", "合成小院")]
        return self.send_page(page(1, values, session=session, total=1), session=session)

    def preview(self, *, at=None):
        return self.call("POST", self.catalog, policy(categories=("residence",)),
                         params={"region": "贵阳", "at": at or self.at, "revision": 4,
                                 "plan_id": "hk-catalog"})

    def publish(self, preview, expected=0, *, status=200):
        return self.call("POST", "/packages", {
            "package_id": self.package_id,
            "region": "贵阳",
            "policy": policy(categories=("residence",)),
            "requested_at": preview["as_of"],
            "expected_revision": 4,
            "plan_id": "hk-catalog",
            "expected_package_revision": expected,
            "preview_digest": preview["preview_digest"],
        }, status=status)

    def test_missing_plan_slot_cannot_borrow_same_region_unassigned_supply(self):
        self.plan()
        self.structured()
        self.add_evidence(evidence("res-proof", "residence", "residence"))
        draft = self.preview(at=stamp(days=-1))
        self.assertEqual(draft["plan"]["gaps"], [{"slot": "directory", "reason": "missing"}])
        self.assertEqual(draft["categories"]["residence"]["active"], [])
        self.assertEqual(self.publish(draft)["state"], "draft")

    def test_working_slot_does_not_use_supply_from_unselected_query(self):
        self.plan()
        self.structured(rows=[item("eligible", "准入小院")])
        self.structured("other", rows=[item("other", "未核对小院")])
        self.add_evidence(evidence("other-proof", "other", "residence"))
        self.assign("hk-001", 0)
        preview = self.preview(at=stamp(days=-1))
        self.assertTrue(preview["plan"]["ready"])
        self.assertEqual(preview["categories"]["residence"]["unverified"], ["eligible"])
        self.assertFalse(preview["ready"])

    def test_plan_checks_all_scope_fields_and_complete_page_set(self):
        self.plan()
        self.open()
        self.send_page(page(1, [item("a", "北院")]))
        self.assertEqual(self.assign("hk-001", 0, status=409)["code"], "plan_unfinished")
        self.open("new-revision", revision=5, pages=1)
        self.send_page(page(1, [item("a", "北院")], session="new-revision", revision=5, total=1),
                       session="new-revision")
        incompatible = self.assign("new-revision", 0, status=409)
        self.assertEqual(incompatible["details"]["fields"], ["source_revision"])

    def test_assignment_race_changes_preview_even_for_equivalent_cards(self):
        self.plan()
        values = [item("residence", "合成小院")]
        self.structured(rows=values)
        self.structured("alternative", rows=values)
        self.add_evidence(evidence("res-proof", "residence", "residence"))
        self.assign("hk-001", 0)
        original = self.preview(at=stamp(days=-1))
        self.assertTrue(original["ready"])
        changed = self.assign("alternative", 1)
        self.assertEqual(changed["generation"], 2)
        self.assertEqual(self.assign("hk-001", 1, status=409)["code"], "assignment_changed")
        self.assertEqual(self.publish(original, status=409)["code"], "preview_changed")
        new = self.preview(at=original["as_of"])
        frozen = self.publish(new)
        self.assertEqual(frozen, self.publish(new))
        self.assertEqual(frozen["assessment"]["plan"]["targets"][0]["session_id"], "alternative")
        history = self.call("GET", "/plans/hk-catalog/slots/directory/history")["history"]
        self.assertEqual([row["session_id"] for row in history], ["hk-001", "alternative"])

    def test_stale_slot_and_optional_missing_target_are_distinct(self):
        optional = {**self.target, "slot": "optional", "source": "backup", "necessity": "optional"}
        self.plan([self.target, optional])
        self.structured()
        self.assign("hk-001", 0)
        at = (datetime.now(timezone.utc) + timedelta(days=4)).isoformat()
        report = self.call("GET", "/plans/hk-catalog/readiness", params={"at": at})
        self.assertEqual(report["gaps"], [{"slot": "directory", "reason": "stale_capture"}])
        self.assertEqual(report["targets"][1]["state"], "missing")


class ArchiveTest(ApiCase):
    def setUp(self):
        super().setUp()
        db_path = self.temp.name + "/stay.sqlite3"
        reader = FolderReader(FIXTURES)
        completed = collector.run(Store(db_path), "archive-source", FIXTURES.as_uri(), reader, 10)
        self.assertTrue(completed["complete"])
        proof = evidence("approved", "stay-1", "residence")
        proof["issued_at"] = "2026-11-19T00:00:00Z"
        proof["expires_at"] = "2026-12-20T00:00:00Z"
        self.add_evidence(proof)
        self.frozen = self.call("POST", "/packages", {
            "package_id": "north-guide", "region": "贵阳", "policy": policy(categories=("residence",)),
            "requested_at": AS_OF, "expected_revision": 4,
        })
        self.assertEqual(self.frozen["state"], "published")

    def archive(self):
        response = self.client.get("/packages/north-guide/archive", params={"revision": 1})
        self.assertEqual(response.status_code, 200, response.text[:200])
        return response.content

    def test_archive_contains_only_its_frozen_html_references(self):
        blob = self.archive()
        self.assertEqual(blob, self.archive())
        with ZipFile(BytesIO(blob)) as zipper:
            self.assertEqual(set(zipper.namelist()), {
                "manifest.json", "package.json", "witnesses.csv", "pages/archive-source/1.html",
            })
        report = bundles.verify(blob)
        self.assertTrue(report["valid"])
        self.assertEqual(report["digest"], self.frozen["digest"])

    def test_frozen_export_remains_byte_identical_after_new_proof(self):
        before = self.archive()
        info = evidence("late-certified", "stay-1", "residence")
        info["issued_at"] = "2026-11-20T00:00:00Z"
        info["expires_at"] = "2027-01-01T00:00:00Z"
        self.add_evidence(info)
        self.assertEqual(self.archive(), before)
        historical = self.call("GET", "/packages/north-guide/export", params={"revision": 1})
        self.assertEqual(historical, self.frozen)

    def test_archive_detects_replaced_html_bytes_even_with_valid_zip_crc(self):
        blob = self.archive()
        with ZipFile(BytesIO(blob)) as zipper:
            parts = [bundles.Part(name, zipper.read(name)) for name in zipper.namelist()]
        replaced = [
            bundles.Part(part.name, part.content + b" ")
            if part.name.endswith(".html") else part for part in parts
        ]
        report = bundles.verify(bundles.encode(replaced))
        self.assertFalse(report["valid"])
        self.assertEqual({entry["code"] for entry in report["issues"]},
                         {"file_digest", "page_reference"})

    def test_digest_binds_published_assessment_and_exact_document(self):
        value = self.call("GET", "/packages/north-guide")
        present = value.pop("digest")
        self.assertEqual(present, digest(value))
        self.assertEqual(value["items"], value["assessment"]["categories"])

    def test_region_report_exposes_source_page_checkpoints_and_proof_gaps(self):
        report = self.call("POST", "/quality", policy(categories=("medical",)), params={
            "region": "贵阳", "revision": 4, "at": AS_OF,
        })
        self.assertEqual(report["collection_states"], {"complete": 1})
        self.assertEqual(report["source_inventory"]["partner-map"]["page_count"], 2)
        self.assertEqual(report["follow_up"][0]["items"], ["med-1"])
        self.assertEqual(report["follow_up"][0]["page_evidence"][0]["page_no"], 1)

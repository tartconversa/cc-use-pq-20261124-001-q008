from datetime import datetime, timedelta, timezone

from tests.support import ApiCase, evidence, item, page, policy


class CoverageTest(ApiCase):
    def setup_catalog(self):
        self.open(pages=1)
        rows = [item("stay-1", "云上小院", "residence"), item("med-1", "社区诊所", "medical"),
                item("food-1", "长者餐厅", "food"), item("lang-1", "粤语管家", "language")]
        self.send_page(page(1, rows, total=1, has_next=False))
        for value in rows:
            self.add_evidence(evidence("ev-" + value["item_id"], value["item_id"], value["category"]))

    def test_verified_coverage_creates_published_package(self):
        self.setup_catalog()
        at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        preview = self.call("POST", "/coverage/preview", params={"region": "贵阳", "revision": 4, "at": at}, data=policy())
        self.assertTrue(preview["ready"])
        package = self.call("POST", "/packages", {"package_id": "pkg-001", "region": "贵阳",
            "policy": policy(), "requested_at": at, "expected_revision": 4})
        self.assertEqual(package["state"], "published")
        self.assertEqual(self.call("GET", "/packages/pkg-001")["digest"], package["digest"])

    def test_missing_category_is_draft_and_gap_is_explainable(self):
        self.open(pages=1)
        row = item("stay-1", "云上小院")
        self.send_page(page(1, [row], total=1, has_next=False))
        at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        result = self.call("POST", "/coverage/preview", params={"region": "贵阳", "revision": 4, "at": at}, data=policy())
        self.assertFalse(result["ready"])
        self.assertEqual({gap["category"] for gap in result["gaps"]}, {"residence", "medical", "food", "language"})
        package = self.call("POST", "/packages", {"package_id": "pkg-002", "region": "贵阳",
            "policy": policy(), "requested_at": at, "expected_revision": 4})
        self.assertEqual(package["state"], "draft")

    def test_expired_evidence_does_not_count(self):
        self.open(pages=1)
        row = item("med-1", "社区诊所", "medical")
        self.send_page(page(1, [row], total=1, has_next=False))
        old = evidence("ev-med-1", "med-1", "medical", days=100, expires=1)
        self.add_evidence(old)
        at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        result = self.call("POST", "/coverage/preview", params={"region": "贵阳", "revision": 4, "at": at}, data=policy(categories=("medical",)))
        self.assertFalse(result["ready"])
        self.assertEqual(result["gaps"][0]["reason"], "minimum_active_items")

    def test_pending_evidence_is_distinct_from_no_item(self):
        self.open(pages=1)
        row = item("med-1", "社区诊所", "medical", status="pending")
        self.send_page(page(1, [row], total=1, has_next=False))
        at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        result = self.call("POST", "/coverage/preview", params={"region": "贵阳", "revision": 4, "at": at}, data=policy(categories=("medical",)))
        reasons = {gap["reason"] for gap in result["gaps"]}
        self.assertEqual(reasons, {"minimum_active_items", "pending_evidence"})

    def test_revision_compare_keeps_old_package(self):
        self.setup_catalog()
        at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        first = self.call("POST", "/packages", {"package_id": "pkg-003", "region": "贵阳",
            "policy": policy(), "requested_at": at, "expected_revision": 4})
        self.assertEqual(first["revision"], 1)
        second = self.call("POST", "/packages", {"package_id": "pkg-003", "region": "贵阳",
            "policy": policy(categories=("residence",)), "requested_at": at, "expected_revision": 4})
        self.assertEqual(second["revision"], 2)
        changed = self.call("GET", "/packages/pkg-003/compare", params={"before": 1, "after": 2})
        self.assertEqual((changed["before"], changed["after"]), (1, 2))
        self.assertNotEqual(self.call("GET", "/packages/pkg-003", params={"revision": 1})["digest"], second["digest"])


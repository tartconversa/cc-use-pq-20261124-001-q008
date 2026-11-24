from tests.support import ApiCase, item, page


class CrawlTest(ApiCase):
    def test_pages_are_contiguous_and_repeatable(self):
        self.open()
        first = self.send_page(page(1, [item("stay-1", "云上小院")]))
        self.assertFalse(first["replayed"])
        repeat = self.send_page(page(1, [item("stay-1", "云上小院")]))
        self.assertTrue(repeat["replayed"])
        gap = self.send_page(page(2, [item("stay-2", "温泉公寓")]), status=200)
        self.assertTrue(gap["complete"])
        report = self.call("GET", "/sessions/hk-001")
        self.assertEqual((report["state"], report["received_pages"]), ("complete", 2))

    def test_gap_and_source_change_stop_mixing(self):
        self.open()
        self.send_page(page(2, [item("stay-2", "温泉公寓")]), status=409)
        self.send_page(page(1, [item("stay-1", "云上小院")]))
        changed = page(2, [item("stay-2", "温泉公寓")], revision=5)
        result = self.send_page(changed, status=409)
        self.assertEqual(result["code"], "source_changed")
        self.assertEqual(self.call("GET", "/sessions/hk-001")["state"], "stale")

    def test_changed_duplicate_page_is_rejected(self):
        self.open(pages=1)
        self.send_page(page(1, [item("stay-1", "云上小院")], total=1, has_next=False))
        changed = page(1, [item("stay-1", "另一套房")], total=1, has_next=False)
        result = self.send_page(changed, status=409)
        self.assertEqual(result["code"], "page_changed")

    def test_abort_preserves_pages_and_blocks_followup(self):
        self.open()
        self.send_page(page(1, [item("stay-1", "云上小院")]))
        result = self.call("POST", "/sessions/hk-001/abort", params={"reason": "页面变更"})
        self.assertEqual(result["state"], "failed")
        self.send_page(page(2, [item("stay-2", "温泉公寓")]), status=409)
        self.assertEqual(self.call("GET", "/sessions/hk-001/pages")["pages"][0]["page_no"], 1)

    def test_page_contract_rejects_bad_bounds(self):
        self.open(pages=1)
        bad = page(1, [item("stay-1", "云上小院")], total=2, has_next=True)
        bad["next_token"] = None
        self.send_page(bad, status=422)


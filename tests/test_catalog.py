from tests.support import ApiCase, item, page


class CatalogTest(ApiCase):
    def test_ingest_deduplicates_identity_but_keeps_different_providers(self):
        self.open(pages=1)
        rows = [item("stay-1", "云上小院"), item("stay-2", "云上小院", provider="partner-b")]
        self.send_page(page(1, rows, total=1, has_next=False))
        catalog = self.call("GET", "/catalog")
        self.assertEqual(len(catalog["items"]), 2)
        identities = self.call("GET", "/identities")["items"]
        self.assertEqual(len(identities), 2)

    def test_same_provider_title_revision_updates_identity_aliases(self):
        self.open(pages=1)
        self.send_page(page(1, [item("stay-1", "云上小院")], total=1, has_next=False))
        self.assertEqual(self.send_page(page(1, [item("stay-1", "云上小院")], total=1, has_next=False))["replayed"], True)
        identities = self.call("GET", "/identities")["items"]
        self.assertEqual(identities[0]["aliases"], ["stay-1"])

    def test_invalid_row_rolls_back_whole_page(self):
        self.open(pages=1)
        invalid = item("stay-1", "云上小院")
        invalid["category"] = "unknown"
        self.send_page(page(1, [invalid], total=1, has_next=False), status=422)
        self.assertEqual(self.call("GET", "/catalog")["items"], [])

    def test_catalog_can_filter_region_and_revision(self):
        self.open(pages=1)
        self.send_page(page(1, [item("stay-1", "云上小院", region="贵阳"), item("stay-2", "温泉公寓", region="安顺")], total=1, has_next=False))
        self.assertEqual(len(self.call("GET", "/catalog", params={"region": "安顺"})["items"]), 1)
        self.assertEqual(len(self.call("GET", "/catalog", params={"revision": 5})["items"]), 0)


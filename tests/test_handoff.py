from tests.support import ApiCase, evidence, item, page, policy, stamp


class HandoffTest(ApiCase):
    def setUp(self):
        super().setUp()
        self.open(pages=1)
        self.send_page(page(1, [item("stay-1", "云上小院")], total=1))
        self.add_evidence(evidence("verified", "stay-1", "residence"))
        self.destination = "hk-operator"
        self.path = "/deliveries/" + self.destination
        self.at = stamp(days=-1)
        self.first = self.freeze(0)

    def freeze(self, expected):
        return self.call("POST", "/packages", {
            "package_id": "handbook", "region": "贵阳", "requested_at": self.at,
            "policy": policy(categories=("residence",)), "expected_revision": 4,
            "expected_package_revision": expected,
        })

    def queue(self, delivery, edition, sequence, *, status=200):
        return self.call("POST", "/deliveries", {
            "destination": self.destination, "delivery_id": delivery, "package_id": "handbook",
            "edition": edition, "expected_sequence": sequence,
        }, status=status)

    def confirm(self, delivery, document, reference, *, status=200):
        return self.call("POST", self.path + "/acknowledge", {
            "delivery_id": delivery, "receiver_reference": reference, "package_digest": document,
        }, status=status)

    def test_destination_reads_exact_edition_and_advances_only_after_matching_receipt(self):
        queued = self.queue("first", 1, 0)
        second = self.freeze(1)
        self.assertEqual(self.queue("next", 2, 1, status=409)["code"], "delivery_unconfirmed")
        self.assertEqual(self.confirm("first", second["digest"], "ref-a", status=409)["code"], "receipt_digest")
        pending = self.call("GET", self.path + "/pending")["pending"]
        self.assertEqual(pending, queued)
        self.assertEqual(self.call("GET", self.path + "/content/first"), self.first)
        confirmed = self.confirm("first", self.first["digest"], "ref-a")
        self.assertEqual(self.confirm("first", self.first["digest"], "ref-a"), confirmed)
        self.assertIsNone(self.call("GET", self.path + "/pending")["pending"])
        follow = self.queue("next", 2, 1)
        self.assertEqual(follow["sequence"], 2)
        history = self.call("GET", self.path, params={"since": 0, "limit": 1})
        self.assertEqual(history["items"], [confirmed])
        self.assertEqual(history["next_sequence"], 1)

    def test_replaying_old_delivery_returns_old_receipt_without_queuing_it(self):
        self.queue("first", 1, 0)
        confirmed = self.confirm("first", self.first["digest"], "ref-a")
        self.assertEqual(self.queue("first", 1, 0), confirmed)
        self.assertEqual(self.queue("renamed", 1, 1, status=409)["code"], "delivery_edition")
        history = self.call("GET", self.path, params={"since": 0})
        self.assertEqual(len(history["items"]), 1)

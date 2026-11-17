# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Two imports for the same vendor storing the same identifier at once.

When two import jobs both insert the same `(partner_id, kind, value)` row, the
loser must skip it instead of breaking the post-processing savepoint. A real
two-transaction race cannot run inside a `TransactionCase`, so these tests
reproduce what the loser sees: a row that exists but its `search` did not return.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestIdentifierRace(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Identifier = cls.env["goodahead.partner.identifier"].sudo()
        cls.vendor = cls.env["res.partner"].create(
            {"name": "Race Test Vendor", "is_company": True}
        )
        # The vendor's contact hook may already derive rows; start from none.
        cls.Identifier.with_context(active_test=False).search(
            [("partner_id", "=", cls.vendor.id)]
        ).unlink()

    def _rows(self):
        return set(
            self.Identifier.with_context(active_test=False)
            .search([("partner_id", "=", self.vendor.id)])
            .mapped(lambda r: (r.kind, r.value))
        )

    def _blind_search(self):
        """`search` that cannot see the rows, as an uncommitted competitor's
        rows are invisible to the loser's snapshot."""
        model_cls = type(self.env["goodahead.partner.identifier"])
        original = model_cls.search

        def search(records, domain, *args, **kwargs):
            if domain == [("partner_id", "in", [self.vendor.id])]:
                return records.browse()
            return original(records, domain, *args, **kwargs)

        return patch.object(model_cls, "search", search)

    def test_new_rows_are_all_created_in_one_go(self):
        pairs = [("phone", "2125550142"), ("domain", "race-vendor.example")]
        created = self.Identifier._add_pairs({self.vendor.id: pairs}, "test")
        self.assertEqual(
            {(kind, value) for _pid, kind, value in created}, set(pairs)
        )
        self.assertEqual(self._rows(), set(pairs))

    def test_row_stored_by_a_competitor_is_skipped_not_raised(self):
        # The competitor got there first.
        self.Identifier.create(
            {
                "partner_id": self.vendor.id,
                "kind": "phone",
                "value": "2125550142",
                "source": "the other import",
            }
        )
        with self._blind_search():
            created = self.Identifier._add_pairs(
                {
                    self.vendor.id: [
                        ("phone", "2125550142"),
                        ("domain", "race-vendor.example"),
                    ]
                },
                "the losing import",
            )

        # Only the row nobody else stored is reported as learned...
        self.assertEqual(
            created, [(self.vendor.id, "domain", "race-vendor.example")]
        )
        # ...both rows exist exactly once, and the competitor's is untouched.
        self.assertEqual(
            self._rows(),
            {("phone", "2125550142"), ("domain", "race-vendor.example")},
        )
        phone = self.Identifier.search(
            [("partner_id", "=", self.vendor.id), ("kind", "=", "phone")]
        )
        self.assertEqual(phone.source, "the other import")

    def test_the_transaction_stays_usable_after_the_conflict(self):
        """The caller's own savepoint must stay usable after the conflict."""
        self.Identifier.create(
            {"partner_id": self.vendor.id, "kind": "phone", "value": "2125550142"}
        )
        with self.env.cr.savepoint():
            with self._blind_search():
                self.Identifier._add_pairs(
                    {self.vendor.id: [("phone", "2125550142")]}, "loser"
                )
            # Work the caller does after learning identifiers still lands.
            self.vendor.comment = "post-processing continued"
        self.vendor.invalidate_recordset(["comment"])
        self.assertIn("post-processing continued", self.vendor.comment or "")

    def test_every_row_taken_returns_nothing_learned(self):
        self.Identifier.create(
            {"partner_id": self.vendor.id, "kind": "phone", "value": "2125550142"}
        )
        with self._blind_search():
            created = self.Identifier._add_pairs(
                {self.vendor.id: [("phone", "2125550142")]}, "loser"
            )
        self.assertEqual(created, [])

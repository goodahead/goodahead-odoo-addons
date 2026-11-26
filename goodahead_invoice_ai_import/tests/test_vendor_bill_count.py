# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The vendor's posted-bill count shown beside the Vendor field on an imported bill.

It helps the user confirm the bill landed on the right contact. Each test carries a
control that would fail if the number were hard-coded. `setUp` makes any call to the
Claude extractor fail. Fake `ZZ…` names avoid matching existing vendors.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_ai_import.lib import claude_client
from odoo.addons.goodahead_invoice_ai_import.tests.posting import post_bill as _post


@tagged("post_install", "-at_install")
class TestVendorBillCount(TransactionCase):
    """`account.move.ai_vendor_bill_count` and the helper underneath it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.AM = cls.env["account.move"]
        cls.Partner = cls.env["res.partner"]
        cls.Import = cls.env["goodahead.invoice.import"]
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", cls.env.company.id)],
            limit=1,
        )
        cls.vendor = cls.Partner.create({"name": "ZZBillCount Vendor", "is_company": True})
        cls.other = cls.Partner.create({"name": "ZZBillCount Other", "is_company": True})

    def setUp(self):
        super().setUp()

        def _forbidden(*args, **kwargs):
            raise AssertionError(
                "a test reached the Claude extractor — nothing in this file may "
                "call the API, and a path that now does is a real defect"
            )

        guard = patch.object(
            claude_client.ClaudeInvoiceExtractor, "extract_invoice", _forbidden
        )
        guard.start()
        self.addCleanup(guard.stop)

    # -- helpers -------------------------------------------------------

    def _bill(self, partner, post=False, date="2026-10-01"):
        bill = self.AM.create(
            {
                "move_type": "in_invoice",
                "partner_id": partner.id,
                "journal_id": self.journal.id,
                "invoice_date": date,
                "invoice_line_ids": [
                    (0, 0, {"name": "a line", "quantity": 1, "price_unit": 10.0}),
                ],
            }
        )
        if post:
            _post(bill)
        return bill

    def _imported(self, partner, post=False):
        """A bill with an `ai.invoice.import` row pointing at it."""
        bill = self._bill(partner, post=post, date="2026-10-05")
        self.Import.create({"name": "zz-bill-count.pdf", "move_id": bill.id})
        return bill

    # -- the count itself ----------------------------------------------

    def test_a_vendor_with_history_reports_its_bills(self):
        """A vendor with two posted bills reads 2; one without bills reads 0."""
        self._bill(self.vendor, post=True)
        self._bill(self.vendor, post=True, date="2026-10-02")
        with_history = self._imported(self.vendor)
        without_history = self._imported(self.other)

        self.assertEqual(with_history.ai_vendor_bill_count, 2)
        self.assertEqual(
            without_history.ai_vendor_bill_count,
            0,
            "a vendor we have never had a bill from must read 0 — that is the "
            "state the whole feature exists to make visible",
        )

    def test_the_bill_being_looked_at_and_drafts_are_not_counted(self):
        """Only other posted bills count: not the bill itself and not drafts."""
        self._bill(self.vendor, post=True)  # real history
        self._bill(self.vendor, post=False, date="2026-10-03")  # a draft
        this_one = self._imported(self.vendor, post=True)

        self.assertEqual(
            this_one.ai_vendor_bill_count,
            1,
            "the bill on screen must not count itself, and a draft is not "
            "history — only the one posted bill is",
        )

        # Control: a second posted bill moves the number.
        self._bill(self.vendor, post=True, date="2026-10-04")
        this_one.invalidate_recordset(["ai_vendor_bill_count"])
        self.assertEqual(this_one.ai_vendor_bill_count, 2)

    def test_it_counts_the_commercial_partner_not_the_contact(self):
        """A bill on a child contact counts for the parent; other vendors stay out."""
        child = self.Partner.create(
            {"name": "ZZBillCount Vendor — AP desk", "parent_id": self.vendor.id}
        )
        self._bill(child, post=True)
        self._bill(self.other, post=True, date="2026-10-02")

        on_parent = self._imported(self.vendor)
        self.assertEqual(on_parent.ai_vendor_bill_count, 1)

        on_other = self._imported(self.other)
        self.assertEqual(
            on_other.ai_vendor_bill_count,
            1,
            "the unrelated vendor has exactly its own one bill — if it read 2 "
            "the two vendors' histories had been pooled",
        )

    # -- the two screens must agree ------------------------------------

    def test_the_candidate_cards_and_the_form_line_give_the_same_number(self):
        """The candidate card and the form line both come from `_ai_posted_bill_counts`."""
        self._bill(self.vendor, post=True)
        self._bill(self.vendor, post=True, date="2026-10-02")

        candidates = self.AM._ai_vendor_candidates({"name": "ZZBillCount Vendor"})
        card = next(c for c in candidates if c["partner"] == self.vendor)

        bill = self._imported(self.vendor)

        self.assertEqual(card["bill_count"], 2)
        self.assertEqual(bill.ai_vendor_bill_count, card["bill_count"])

    # -- only on an imported bill --------------------------------------

    def test_only_a_bill_from_the_import_carries_the_number(self):
        """A manually entered bill shows no count until an import row points at it."""
        self._bill(self.vendor, post=True)
        by_hand = self._bill(self.vendor)

        self.assertFalse(by_hand.ai_from_import)
        self.assertEqual(
            by_hand.ai_vendor_bill_count,
            0,
            "nothing is counted for a bill the import did not create",
        )

        self.Import.create({"name": "zz-bill-count.pdf", "move_id": by_hand.id})
        by_hand.invalidate_recordset(["ai_from_import", "ai_vendor_bill_count"])

        self.assertTrue(by_hand.ai_from_import)
        self.assertEqual(by_hand.ai_vendor_bill_count, 1)

    # -- multi-company --------------------------------------------------

    def test_the_count_is_scoped_to_one_company(self):
        """In a multi-company database a vendor's bills count only in their own company.

        Uses an existing second company: creating one can fail when
        `stock_dropshipping` is installed and the company has no warehouse.
        """
        elsewhere = self.env["res.company"].search(
            [("id", "!=", self.env.company.id)], limit=1
        )
        if not elsewhere:
            self.skipTest(
                "single-company database — there is no second company to ask "
                "about, so this assertion has nothing to say here"
            )
        self._bill(self.vendor, post=True)

        own = self.AM._ai_posted_bill_counts(self.vendor, company=self.env.company)
        foreign = self.AM._ai_posted_bill_counts(self.vendor, company=elsewhere)

        self.assertEqual(own[self.vendor], 1)
        self.assertEqual(
            foreign[self.vendor],
            0,
            "the bill belongs to another company and must not be counted here",
        )

    def test_the_helper_answers_for_every_partner_it_was_asked_about(self):
        """A vendor with no bills comes back as 0 rather than missing from the dict."""
        self._bill(self.vendor, post=True)
        counts = self.AM._ai_posted_bill_counts(self.vendor | self.other)

        self.assertEqual(counts[self.other], 0)
        self.assertEqual(counts[self.vendor], 1)


@tagged("post_install", "-at_install")
class TestTheCountReadsAsEnglish(TransactionCase):
    """Singular and plural "posted bill(s)" in the chatter note and the bill form.

    Odoo's `_()` has no plural form, so the form uses two spans switched by
    `invisible=`; they are checked through `get_view`.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.AM = cls.env["account.move"]
        cls.Partner = cls.env["res.partner"]
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", cls.env.company.id)],
            limit=1,
        )
        cls.vendor = cls.Partner.create(
            {"name": "ZZPluralProbe Refrigeration", "is_company": True}
        )

    def setUp(self):
        super().setUp()

        def _forbidden(*args, **kwargs):
            raise AssertionError(
                "a test reached the Claude extractor — nothing in this file may "
                "call the API, and a path that now does is a real defect"
            )

        guard = patch.object(
            claude_client.ClaudeInvoiceExtractor, "extract_invoice", _forbidden
        )
        guard.start()
        self.addCleanup(guard.stop)

    def _post_a_bill(self, partner, date="2026-10-01"):
        bill = self.AM.create(
            {
                "move_type": "in_invoice",
                "partner_id": partner.id,
                "journal_id": self.journal.id,
                "invoice_date": date,
                "invoice_line_ids": [
                    (0, 0, {"name": "a line", "quantity": 1, "price_unit": 10.0}),
                ],
            }
        )
        _post(bill)
        return bill

    def _note_for_an_unrecognised_vendor(self):
        invoice = self.AM.create(
            {"move_type": "in_invoice", "journal_id": self.journal.id}
        )
        self.AM._ai_post_unrecognised_vendor(
            invoice, {"name": "ZZPluralProbe", "country_code": "US"}
        )
        return "\n".join((m.body or "") for m in invoice.message_ids)

    def test_one_bill_reads_as_one_bill(self):
        """One bill reads "1 posted bill", not "1 posted bills"."""
        self._post_a_bill(self.vendor)
        body = self._note_for_an_unrecognised_vendor()
        self.assertIn(
            "ZZPluralProbe Refrigeration",
            body,
            "precondition: the vendor has to be offered as a candidate at all, "
            "or this test passes by finding neither string",
        )
        self.assertIn("1 posted bill", body)
        self.assertNotIn("1 posted bills", body)

    def test_two_bills_read_as_two_bills(self):
        """Control: two bills use the plural."""
        self._post_a_bill(self.vendor)
        self._post_a_bill(self.vendor, date="2026-10-02")
        body = self._note_for_an_unrecognised_vendor()
        self.assertIn("2 posted bills", body)

    def test_the_form_has_one_span_for_one_and_one_for_more(self):
        """The form has exactly one singular and one plural span with opposite conditions."""
        from lxml import etree  # noqa: PLC0415

        arch = etree.fromstring(
            self.AM.get_view(self.env.ref("account.view_move_form").id, "form")[
                "arch"
            ]
        )
        self.assertTrue(
            arch.xpath("//field[@name='ai_vendor_bill_count']"),
            "the count is not in the bill form at all",
        )
        singular = arch.xpath("//span[@invisible='ai_vendor_bill_count != 1']")
        plural = arch.xpath("//span[@invisible='ai_vendor_bill_count == 1']")
        self.assertEqual(len(singular), 1, "the singular span")
        self.assertEqual(len(plural), 1, "the plural span")
        self.assertEqual(singular[0].text, "posted bill from this vendor already.")
        self.assertEqual(plural[0].text, "posted bills from this vendor already.")
        # The conditions must differ, or a count of 1 renders both sentences.
        self.assertNotEqual(
            singular[0].get("invisible"),
            plural[0].get("invisible"),
            "both spans carry the same condition, so they render together",
        )

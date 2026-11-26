# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The vendor's own history, shown beside the Vendor field on an imported bill
(19.0.3.2.0).

The request: a number on or beside the vendor's line saying how many vendor
bills this vendor has, so that a user can always be sure it is the right
contact.

🔴 EVERY TEST HERE CARRIES A CONTROL — something in the same test that would
FAIL if the number under test were hard-coded, or if the rule it checks were
made unconditional. The recurring defect in this project is a check that reads a
proxy and fails in the direction that looks like success, so "it counted 2" is
never asserted without "and this other vendor counted 0".

🔴 NOTHING HERE MAY REACH THE CLAUDE API. `setUp` replaces
`ClaudeInvoiceExtractor.extract_invoice` with a function that raises, the same
guard `test_vendor_identifiers.py` uses. ⚠️ The older `lib.claude_client.requests`
patch named in some notes no longer exists and must not be reintroduced: this
module has owned no HTTP transport since 19.0.1.2.0.

Fake `ZZ…` names throughout, so an assertion that touches the database can never
collide with a real vendor — these tests also run against a migrated test
database with several hundred suppliers.
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
        """A bill that CAME FROM THE AI IMPORT — i.e. one with an
        `ai.invoice.import` row pointing at it. That row is the whole test the
        feature uses to decide whether to show anything."""
        bill = self._bill(partner, post=post, date="2026-10-05")
        self.Import.create({"name": "zz-bill-count.pdf", "move_id": bill.id})
        return bill

    # -- the count itself ----------------------------------------------

    def test_a_vendor_with_history_reports_its_bills(self):
        """CONTROL: the same assertion run against a vendor with NO bills must
        read 0. A hard-coded 2, or a count that ignored the partner, would pass
        the first half and fail the second."""
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
        """Three bills exist for the vendor: this one, a draft one, and one
        posted earlier. Only the last is history.

        CONTROL, in this same test: the posted one IS counted, so a compute that
        simply returned 0 — or one that dropped every bill rather than only this
        one — could not pass.
        """
        self._bill(self.vendor, post=True)  # real history
        self._bill(self.vendor, post=False, date="2026-10-03")  # a draft
        this_one = self._imported(self.vendor, post=True)

        self.assertEqual(
            this_one.ai_vendor_bill_count,
            1,
            "the bill on screen must not count itself, and a draft is not "
            "history — only the one posted bill is",
        )

        # The control, stated as its own assertion rather than left implicit: a
        # SECOND posted bill moves the number, which a stubbed 1 would not do.
        self._bill(self.vendor, post=True, date="2026-10-04")
        this_one.invalidate_recordset(["ai_vendor_bill_count"])
        self.assertEqual(this_one.ai_vendor_bill_count, 2)

    def test_it_counts_the_commercial_partner_not_the_contact(self):
        """A bill filed against a child contact is history for the PARENT — that
        is what `commercial_partner_id` means, and it is the number a person
        checking "is this the right contact" wants.

        CONTROL: an unrelated vendor's bills stay out of it. A count that had
        quietly dropped the partner clause would pass the first assertion by
        accident and fail this one.
        """
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
        """🔴 THE TEST THAT STOPS THEM DRIFTING. The candidate card and this
        line sit a few pixels apart on the same form: a person picks a card
        reading "2 bills" and must then see 2 on the line that replaces it. Both
        now come from `_ai_posted_bill_counts`; this asserts they still do.

        CONTROL: the number is also asserted to be the independently known 2, so
        two equally broken implementations agreeing on 0 cannot pass.
        """
        self._bill(self.vendor, post=True)
        self._bill(self.vendor, post=True, date="2026-10-02")

        candidates = self.AM._ai_vendor_candidates({"name": "ZZBillCount Vendor"})
        card = next(c for c in candidates if c["partner"] == self.vendor)

        bill = self._imported(self.vendor)

        self.assertEqual(card["bill_count"], 2)
        self.assertEqual(bill.ai_vendor_bill_count, card["bill_count"])

    # -- only on an imported bill --------------------------------------

    def test_only_a_bill_from_the_import_carries_the_number(self):
        """A bill typed by a person had its vendor chosen by that person, so
        there is nothing to double-check and the line would be noise on every
        manual bill in the database.

        CONTROL: the very same bill, once an `ai.invoice.import` row points at
        it, flips `ai_from_import` and produces the count — so the flag cannot
        be a constant in either direction.
        """
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
        """🔴 ALPHA AND BETA BUY FROM EACH OTHER, so a vendor can have a long
        history in one company and none at all in the other. A count that
        answered "every company the reader can see" would show a reassuring
        number on a bill for a company that has never bought from that vendor —
        the exact wrong answer to *"is this the right contact"*.

        Asserted on the helper against an EXISTING second company, not a created
        one: `res.company.create` in this database runs
        `stock_dropshipping._create_dropship_rule`, which fails its own
        `_check_company_consistency` on a company with no warehouse — measured,
        it is what the first version of this test died on. The second company is
        also the real shape of the problem (Alpha and Beta), so reading it is better
        evidence than minting a third.

        CONTROL: the same call with the bill's own company returns 1, so a
        domain that matched nothing at all could not pass.
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
        """A vendor with no bills must come back as 0 rather than be absent —
        the callers index the result directly, and a missing key is how a 0
        turns into a traceback on the one screen that exists for 0.

        CONTROL: the vendor that does have a bill reads 1 in the same dict.
        """
        self._bill(self.vendor, post=True)
        counts = self.AM._ai_posted_bill_counts(self.vendor | self.other)

        self.assertEqual(counts[self.other], 0)
        self.assertEqual(counts[self.vendor], 1)


@tagged("post_install", "-at_install")
class TestTheCountReadsAsEnglish(TransactionCase):
    """🔴 *"1 posted bills"*, SEEN ON STAGING, AND SHIPPED WITH NO TEST.

    19.0.3.18.0 fixed it in two places — the chatter line in
    `_ai_post_unrecognised_vendor` and the two `<span>`s in the bill form — in a
    commit whose headline was "29 new tests"; `grep -rn "posted bill" tests/`
    found three hits, all of them sentences in docstrings. A review.

    The XML half is the class this project has been bitten by repeatedly: an
    `invisible=` expression that stops matching renders BOTH spans or NEITHER,
    the module installs, every gate stays green and the line reads
    *"1 posted bill posted bills from this vendor already."* So it is asserted
    through `get_view`, which is what the client actually receives.

    `_()` genuinely has no plural in 19.0 — there is no `ngettext` anywhere in
    `odoo/tools/translate.py` — so two separate strings is the right shape.
    What was missing is a red.
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
        """🔴 THE RED, as it was seen: *"1 posted bills"*."""
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
        """🔴 THE CONTROL. A fix that hard-coded the singular would pass the
        test above and would print "2 posted bill"."""
        self._post_a_bill(self.vendor)
        self._post_a_bill(self.vendor, date="2026-10-02")
        body = self._note_for_an_unrecognised_vendor()
        self.assertIn("2 posted bills", body)

    def test_the_form_has_one_span_for_one_and_one_for_more(self):
        """🔴 THE SILENT HALF. QWeb has no plural form and the arch cannot call
        `_()`, so the sentence is two spans switched by `invisible=`. Delete one
        of them, or let an expression stop matching the field, and the bill form
        reads both halves or neither with nothing anywhere failing."""
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
        # The two conditions have to be each other's complement, or a count of
        # 1 renders both sentences. Asserted as a PAIR, because either one on
        # its own is satisfied by two identical spans.
        self.assertNotEqual(
            singular[0].get("invisible"),
            plural[0].get("invisible"),
            "both spans carry the same condition, so they render together",
        )

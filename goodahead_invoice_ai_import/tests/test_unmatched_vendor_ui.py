# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The bill form for an imported bill whose vendor was not recognised.

1. The vendor card reports draft bills next to posted ones.
2. The "vendor not recognised" explanation is shown even when there are no
   candidates.
3. A "Create contact from this invoice" button creates the vendor on request,
   refuses parties that can never be vendors, and never creates duplicates.

Automatic vendor creation stays off; the button is an explicit decision by a
person. These tests check the arch and evaluate `invisible=` modifiers against
records; they cannot check visual layout.
"""

import re
from unittest.mock import patch

from lxml import etree

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools.safe_eval import safe_eval

from odoo.addons.goodahead_invoice_import.adapters.ubl_generator import (
    generate_peppol_invoice_xml,
)
from odoo.addons.goodahead_invoice_ai_import.tests.posting import post_bill


class _UnmatchedHarness(TransactionCase):
    """A bill from a vendor nobody has, with a readable extraction on it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.company.claude_api_key = "test-key"
        cls.Partner = cls.env["res.partner"]
        cls.AM = cls.env["account.move"]
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", cls.company.id)],
            limit=1,
        )

    # -- fixtures ------------------------------------------------------

    def _seller(self, **overrides):
        seller = {
            "name": "ZZ Unmatched Carrier Inc",
            "vat": None,
            "country_code": "DE",
            "city": "Berlin",
            "street": "Beispielweg 7",
            "postal_code": "10115",
            "phone": "+4930555777888",
            "email": "ap@zz-unmatched-carrier.example",
            "website": "zz-unmatched-carrier.example",
            "business_id": "ZZ-REG-4242",
        }
        seller.update(overrides)
        return seller

    def _extracted(self, **seller_overrides):
        return {
            "type": "Invoice",
            "invoice_number": "ZZ-UI-1",
            "issue_date": "2026-09-30",
            "currency": "EUR",
            "seller": self._seller(**seller_overrides),
            "buyer": {
                "name": self.company.name,
                "country_code": self.company.country_id.code or "FI",
            },
            "lines": [
                {
                    "product_code": "ZZ-UI-1",
                    "name": "ZZ Freight",
                    "quantity": 1.0,
                    "unit_code": "C62",
                    "price_unit": 100.0,
                    "line_total": 100.0,
                    "tax_percent": 0.0,
                    "tax_amount": 0.0,
                    "tax_category": "Z",
                }
            ],
            "tax_subtotals": [
                {
                    "tax_percent": 0.0,
                    "taxable_amount": 100.0,
                    "tax_amount": 0.0,
                    "tax_category": "Z",
                }
            ],
            "totals": {
                "line_extension": 100.0,
                "tax_exclusive": 100.0,
                "tax_inclusive": 100.0,
                "payable": 100.0,
            },
        }

    def _run_import(self, extracted=None, **seller_overrides):
        extracted = extracted or self._extracted(**seller_overrides)
        attachment = self.env["ir.attachment"].create(
            {
                "name": "zz-ui.pdf",
                "raw": b"%PDF-1.4 fake",
                "mimetype": "application/pdf",
            }
        )
        record = self.env["goodahead.invoice.import"].create(
            {
                "name": "zz-ui.pdf",
                "company_id": self.company.id,
                "pdf_attachment_id": attachment.id,
            }
        )
        with patch.object(
            type(self.AM),
            "_ai_extract_and_generate_ubl",
            return_value={
                "extracted": extracted,
                "usage": {"input_tokens": 10, "output_tokens": 20},
                "model_name": "test-model",
                "xml_bytes": generate_peppol_invoice_xml(extracted),
            },
        ):
            outcome = record._process_import()
        self.assertTrue(outcome.startswith("OK"), outcome)
        return record.move_id

    def _bill_for(self, partner, state="draft", move_type="in_invoice"):
        move = self.AM.create(
            {
                "move_type": move_type,
                "journal_id": self.journal.id,
                "partner_id": partner.id,
                "invoice_line_ids": [
                    (0, 0, {"name": "x", "quantity": 1, "price_unit": 10})
                ],
            }
        )
        if state == "posted":
            move.invoice_date = "2026-01-01"
            post_bill(move)
        return move

    def _arch(self):
        """The bill form's arch as the client receives it."""
        return self.AM.get_view(
            self.env.ref("account.view_move_form").id, "form"
        )["arch"]

    # ------------------------------------------------------------------
    # `get_view` returns one arch for all records, so "the arch contains X"
    # cannot detect an `invisible=` modifier that is too permissive. The
    # helper below evaluates the modifier against a record's values, as the
    # web client does, so tests can check both the visible and hidden state.
    # ------------------------------------------------------------------

    def _element_invisible_for(self, css_class, record):
        """Evaluate the arch element's `invisible=` against `record`'s values.

        Returns True when the client would HIDE it. Raises when the element is
        absent, so a deleted block cannot read as "hidden".
        """
        tree = etree.fromstring(self._arch())
        nodes = tree.xpath(
            "//*[contains(concat(' ', normalize-space(@class), ' '), ' %s ')]"
            % css_class
        )
        self.assertTrue(
            nodes, "no element carrying class %r in the arch" % css_class
        )
        self.assertEqual(
            len(nodes), 1, "class %r is not unique in the arch" % css_class
        )
        expression = nodes[0].get("invisible")
        if expression in (None, ""):
            return False
        if expression in ("1", "True", "true"):
            return True
        # Only the field names the expression uses are read.
        names = {
            name
            for name in self.AM._fields
            if re.search(r"\b%s\b" % re.escape(name), expression)
        }
        values = {name: record[name] for name in names}
        # A many2one evaluates as its id, as in the client.
        for name, value in list(values.items()):
            if hasattr(value, "_name"):
                values[name] = value.id
        return bool(safe_eval(expression, values))


@tagged("post_install", "-at_install")
class TestTheCardNamesTheDraftCount(_UnmatchedHarness):
    """The vendor card reports draft bills, not only posted ones."""

    def test_the_draft_count_is_reported_beside_the_posted_one(self):
        """Other draft bills are counted separately from posted ones.

        Counts are only computed for imported bills (`ai_from_import`), so the
        bill under test goes through `_run_import`.
        """
        move = self._run_import()
        move.action_ai_create_vendor_from_extraction()
        vendor = move.partner_id
        self._bill_for(vendor)
        self._bill_for(vendor)
        move.invalidate_recordset(
            ["ai_vendor_bill_count", "ai_vendor_draft_bill_count"]
        )

        self.assertEqual(
            move.ai_vendor_draft_bill_count,
            2,
            "two other drafts — and never itself, which is the subtraction",
        )
        self.assertEqual(
            move.ai_vendor_bill_count, 0, "a draft is never counted as posted"
        )

    def test_a_draft_bill_does_not_count_itself(self):
        """The bill on screen, usually a draft, is not counted in its own
        draft count."""
        move = self._run_import()
        move.action_ai_create_vendor_from_extraction()
        move.invalidate_recordset(["ai_vendor_draft_bill_count"])

        self.assertEqual(move.ai_vendor_draft_bill_count, 0)

    def test_control_a_vendor_with_no_bills_at_all_still_reads_zero_and_zero(self):
        """A new vendor reports zero posted and zero draft bills."""
        vendor = self.Partner.create(
            {"name": "ZZ Brand New Vendor", "is_company": True}
        )

        counts = self.AM._ai_bill_counts(vendor, company=self.company)

        self.assertEqual(counts, {vendor: {}})
        self.assertEqual(self.AM._ai_posted_bill_counts(vendor), {vendor: 0})
        self.assertEqual(self.AM._ai_draft_bill_counts(vendor), {vendor: 0})

    def test_control_a_posted_bill_is_still_counted_as_posted(self):
        """The helpers tell posted and draft bills apart."""
        vendor = self.Partner.create(
            {"name": "ZZ Posted Vendor", "is_company": True}
        )
        self._bill_for(vendor, state="posted")
        self._bill_for(vendor)

        counts = self.AM._ai_bill_counts(vendor, company=self.company)

        self.assertEqual(counts[vendor].get("posted"), 1)
        self.assertEqual(counts[vendor].get("draft"), 1)
        self.assertEqual(self.AM._ai_posted_bill_counts(vendor), {vendor: 1})
        self.assertEqual(self.AM._ai_draft_bill_counts(vendor), {vendor: 1})

    def test_one_helper_still_feeds_both_screens(self):
        """Both projections read `_ai_bill_counts`, so stubbing it changes both
        answers.
        """
        vendor = self.Partner.create(
            {"name": "ZZ One Body Vendor", "is_company": True}
        )

        with patch.object(
            type(self.AM),
            "_ai_bill_counts",
            return_value={vendor: {"posted": 41, "draft": 42}},
        ):
            self.assertEqual(
                self.AM._ai_posted_bill_counts(vendor), {vendor: 41}
            )
            self.assertEqual(
                self.AM._ai_draft_bill_counts(vendor), {vendor: 42}
            )

    def test_the_arch_carries_a_draft_clause_on_the_zero_posted_alert(self):
        arch = self._arch()

        self.assertIn("ai_vendor_draft_bill_count", arch)
        self.assertIn("other drafts", arch)

    def test_the_bill_history_block_is_hidden_on_a_bill_nobody_imported(self):
        """The bill-history block is hidden on a hand-typed bill."""
        vendor = self.Partner.create({"name": "ZZ Manual", "is_company": True})
        manual = self.AM.create(
            {
                "move_type": "in_invoice",
                "journal_id": self.journal.id,
                "partner_id": vendor.id,
            }
        )

        self.assertTrue(
            self._element_invisible_for("o_ai_vendor_bill_history", manual),
            "a hand-typed bill had its vendor chosen by a person",
        )

    def test_control_the_bill_history_block_is_shown_on_an_imported_bill(self):
        move = self._run_import()
        move.action_ai_create_vendor_from_extraction()
        move.invalidate_recordset(["ai_from_import"])

        self.assertFalse(
            self._element_invisible_for("o_ai_vendor_bill_history", move),
            "an imported bill with a vendor gets the history line",
        )


@tagged("post_install", "-at_install")
class TestTheExplanationRendersWithoutCandidates(_UnmatchedHarness):
    """The unmatched-vendor explanation does not depend on candidates."""

    def test_the_explanation_is_not_inside_the_candidate_block(self):
        """The zero-candidate explanation is its own block, outside the
        candidate list.
        """
        arch = self._arch()

        self.assertIn("o_ai_vendor_unmatched", arch)
        self.assertIn(
            "This vendor was not recognised and nothing in the contact",
            arch,
            "the zero-candidate wording has to exist as its own string",
        )

    def test_the_explanation_is_hidden_on_a_bill_nobody_imported(self):
        """A new, hand-made bill does not show the explanation or the
        button."""
        manual = self.AM.create(
            {"move_type": "in_invoice", "journal_id": self.journal.id}
        )

        self.assertFalse(manual.partner_id, "fixture check: no vendor")
        self.assertFalse(
            manual.ai_from_import, "fixture check: nobody imported this bill"
        )
        self.assertTrue(
            self._element_invisible_for("o_ai_vendor_unmatched", manual),
            "a bill nobody imported is never told a document was read off it",
        )
        self.assertFalse(
            manual.ai_can_create_vendor,
            "and the button it names is not there either — the two gates agree",
        )

    def test_control_the_explanation_is_shown_on_an_imported_bill(self):
        """An imported bill with no vendor shows the explanation."""
        move = self._run_import()

        self.assertFalse(move.partner_id, "fixture check: nothing matched")
        self.assertTrue(move.ai_from_import, "fixture check: this one WAS imported")
        self.assertFalse(
            self._element_invisible_for("o_ai_vendor_unmatched", move),
            "the bill that needs the explanation gets it",
        )

    def test_control_the_explanation_is_hidden_once_a_vendor_is_filed(self):
        """The third state of the same modifier: imported, but filed."""
        move = self._run_import()
        move.partner_id = self.Partner.create(
            {"name": "ZZ Now Filed", "is_company": True}
        )

        self.assertTrue(
            self._element_invisible_for("o_ai_vendor_unmatched", move),
            "a filed bill has nothing to explain",
        )

    def test_a_bill_with_no_candidates_still_has_something_to_say(self):
        """An import with no match and no candidates still gets the
        explanation and the button."""
        move = self._run_import()

        self.assertFalse(move.partner_id)
        self.assertFalse(
            move.ai_vendor_candidate_ids,
            "fixture check: nothing in this contact book resembles the seller, "
            "so this is the zero-candidate case",
        )
        self.assertTrue(
            move.ai_from_import and move.ai_can_create_vendor,
            "so the block and its button are both reachable",
        )

    def test_control_the_candidate_list_still_renders_when_there_are_candidates(self):
        """The candidate list is hidden without candidates and shown with
        them, alongside the explanation.
        """
        move = self._run_import()
        self.assertTrue(
            self._element_invisible_for("o_ai_vendor_candidates", move),
            "no candidates on this bill, so the list is hidden — and the "
            "explanation beside it is NOT, which is the whole fix",
        )

        self.env["goodahead.vendor.candidate"]._build_for_move(
            move,
            [
                {
                    "partner": self.Partner.create(
                        {"name": "ZZ A Candidate", "is_company": True}
                    ),
                    "reason": "same phone",
                    "bill_count": 0,
                    "draft_bill_count": 0,
                    "archived": False,
                }
            ],
        )
        move.invalidate_recordset(["ai_vendor_candidate_ids"])

        self.assertFalse(
            self._element_invisible_for("o_ai_vendor_candidates", move),
            "with a candidate the list renders",
        )
        self.assertFalse(
            self._element_invisible_for("o_ai_vendor_unmatched", move),
            "and so does the explanation — both, not one or the other",
        )
        self.assertIn("action_use_this", self._arch())

    def test_control_the_explanation_is_gone_once_a_vendor_is_filed(self):
        """Once a vendor is set, the create button is no longer offered."""
        move = self._run_import()
        vendor = self.Partner.create({"name": "ZZ Picked", "is_company": True})

        move.partner_id = vendor

        self.assertFalse(
            move.ai_can_create_vendor,
            "and the button goes with it",
        )


@tagged("post_install", "-at_install")
class TestTheCreateContactButton(_UnmatchedHarness):
    """The "Create contact from this invoice" button."""

    def test_it_creates_one_contact_and_files_the_bill_against_it(self):
        """One contact is created from the extraction and set on the bill,
        without a bank account."""
        move = self._run_import()
        before = self.Partner.with_context(active_test=False).search_count([])

        move.action_ai_create_vendor_from_extraction()

        self.assertEqual(
            self.Partner.with_context(active_test=False).search_count([]),
            before + 1,
            "exactly one contact",
        )
        vendor = move.partner_id
        self.assertTrue(vendor)
        self.assertEqual(vendor.name, "ZZ Unmatched Carrier Inc")
        self.assertEqual(vendor.city, "Berlin")
        self.assertEqual(vendor.zip, "10115")
        self.assertEqual(vendor.country_id, self.env.ref("base.de"))
        self.assertEqual(vendor.company_registry, "ZZ-REG-4242")
        self.assertTrue(vendor.is_company)
        self.assertFalse(
            vendor.bank_ids,
            "a bank account is NEVER written automatically",
        )

    def test_a_tax_id_the_vat_check_refuses_does_not_block_the_contact(self):
        """A Canadian business number is not a VAT number to ``base_vat``.

        The contact is still created, without the tax id; the number waits
        as a suggestion and the chatter says why it was not stored.
        """
        if not self.env["ir.module.module"].search_count(
            [("name", "=", "base_vat"), ("state", "=", "installed")]
        ):
            self.skipTest("base_vat is not installed")
        move = self._run_import(
            name="ZZ Canadian Contractor",
            vat="3225301672",
            country_code="CA",
            city="Toronto",
            postal_code="M5V 2T6",
        )

        move.action_ai_create_vendor_from_extraction()

        vendor = move.partner_id
        self.assertTrue(vendor, "the contact is created despite the tax id")
        self.assertEqual(vendor.name, "ZZ Canadian Contractor")
        self.assertEqual(vendor.country_id, self.env.ref("base.ca"))
        self.assertFalse(vendor.vat, "a refused tax id is not stored")
        suggestion = self.env["goodahead.vendor.data.suggestion"].search(
            [("partner_id", "=", vendor.id), ("field_name", "=", "vat")]
        )
        self.assertEqual(suggestion.invoice_value, "3225301672")
        self.assertTrue(
            any("3225301672" in body for body in move.message_ids.mapped("body")),
            "the chatter names the tax id that was not stored",
        )

    def test_control_a_valid_tax_id_is_stored_on_the_new_contact(self):
        """Control: a tax id the VAT check accepts is stored."""
        move = self._run_import(vat="DE123456788")

        move.action_ai_create_vendor_from_extraction()

        self.assertEqual(move.partner_id.vat, "DE123456788")

    def test_it_learns_the_identifiers_so_the_next_bill_matches_itself(self):
        """The new contact gets phone and domain identifiers."""
        move = self._run_import()

        move.action_ai_create_vendor_from_extraction()

        kinds = set(move.partner_id.identifier_ids.mapped("kind"))
        self.assertIn("phone", kinds)
        self.assertIn("domain", kinds)

    def test_a_second_bill_from_the_same_vendor_creates_nobody(self):
        """A second bill from the same vendor matches the created contact at
        import, via its identifiers, so the button is not even offered.
        """
        move = self._run_import()
        move.action_ai_create_vendor_from_extraction()
        vendor = move.partner_id
        count = self.Partner.with_context(active_test=False).search_count([])

        second = self._run_import(dict(self._extracted(), invoice_number="ZZ-UI-2"))

        self.assertEqual(
            self.Partner.with_context(active_test=False).search_count([]),
            count,
            "a second bill from the same vendor creates nobody",
        )
        self.assertEqual(
            second.partner_id,
            vendor,
            "the import matched the contact the first press created",
        )
        self.assertFalse(
            second.ai_can_create_vendor,
            "so the button is not even offered on it",
        )

    def test_the_button_links_an_existing_contact_instead_of_creating_a_second(self):
        """If a matching contact appeared after import, the button links it
        instead of creating another (`_ai_find_vendor` runs first).
        """
        move = self._run_import()
        self.assertFalse(move.partner_id, "fixture check: nothing matched at import")
        existing = self.Partner.create(
            {"name": "ZZ Unmatched Carrier Inc", "is_company": True}
        )
        count = self.Partner.with_context(active_test=False).search_count([])

        move.action_ai_create_vendor_from_extraction()

        self.assertEqual(move.partner_id, existing)
        self.assertEqual(
            self.Partner.with_context(active_test=False).search_count([]),
            count,
            "nobody was created",
        )
        bodies = " ".join(
            self.env["mail.message"]
            .search([("model", "=", "account.move"), ("res_id", "=", move.id)])
            .mapped("body")
        )
        self.assertIn("already existed", bodies, "and the chatter says why")

    def test_it_records_who_asked_and_from_which_bill(self):
        move = self._run_import()

        move.action_ai_create_vendor_from_extraction()

        bodies = " ".join(
            self.env["mail.message"]
            .search([("model", "=", "account.move"), ("res_id", "=", move.id)])
            .mapped("body")
        )
        self.assertIn(self.env.user.display_name, bodies)
        self.assertIn("created this contact from bill", bodies)

    def test_it_refuses_a_seller_that_resolves_to_our_own_company(self):
        """A seller resolving to the company's own partner is refused and
        nothing is created.
        """
        self.env.company.partner_id.name = "ZZ Our Own Company Oy"
        move = self._run_import(name="ZZ Our Own Company Oy")
        self.assertFalse(move.partner_id, "fixture check: the guard already refused it")
        before = self.Partner.with_context(active_test=False).search_count([])

        with self.assertRaises(UserError) as caught:
            move.action_ai_create_vendor_from_extraction()

        self.assertIn("own company", str(caught.exception))
        self.assertEqual(
            self.Partner.with_context(active_test=False).search_count([]),
            before,
            "and nothing was created",
        )

    def test_it_refuses_a_seller_that_resolves_to_one_of_our_logins(self):
        move = self._run_import(name="ZZ Login Holder Oy")
        holder = self.Partner.create(
            {"name": "ZZ Login Holder Oy", "is_company": True}
        )
        self.env["res.users"].create(
            {
                "name": holder.name,
                "login": "zz-ui-login-holder",
                "partner_id": holder.id,
            }
        )

        with self.assertRaises(UserError) as caught:
            move.action_ai_create_vendor_from_extraction()

        self.assertIn("user account", str(caught.exception))

    def test_it_refuses_a_bill_that_already_has_a_vendor(self):
        """The server-side check holds even when the button is hidden (RPC)."""
        move = self._run_import()
        move.partner_id = self.Partner.create(
            {"name": "ZZ Already Filed", "is_company": True}
        )

        with self.assertRaises(UserError):
            move.action_ai_create_vendor_from_extraction()

    def test_it_refuses_a_bill_with_no_readable_seller_name(self):
        move = self._run_import()
        self.env["goodahead.invoice.import"].search(
            [("move_id", "=", move.id)]
        ).extraction_data = '{"seller": {}}'
        self.env["ir.attachment"].search(
            [
                ("res_model", "=", "account.move"),
                ("res_id", "=", move.id),
                ("name", "=", "extraction.json"),
            ]
        ).unlink()

        self.assertFalse(
            move.ai_can_create_vendor, "and the button is not offered either"
        )
        with self.assertRaises(UserError):
            move.action_ai_create_vendor_from_extraction()

    def test_control_the_offer_is_made_on_the_bill_that_needs_it(self):
        """Control: `ai_can_create_vendor` is True on an unmatched import."""
        move = self._run_import()

        self.assertTrue(move.ai_can_create_vendor)

    def test_the_arch_carries_the_button(self):
        arch = self._arch()

        self.assertIn("action_ai_create_vendor_from_extraction", arch)
        self.assertIn("Create contact from this invoice", arch)
        self.assertIn("ai_can_create_vendor", arch)

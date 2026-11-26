# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Three UI reports from testing the import on a small Finnish test database.

None of them is a crash; all three are the screen saying less than it knows,
which is this module's recurring shape of defect.

  1. **bill D** — the bill said there were no posted bills from the vendor,
     which was right, but said nothing of the vendor's drafts. The vendor had
     **no posted and several draft** bills, and the sentence *"No posted bills
     from this vendor yet"* was true in every word while the conclusion a
     reader draws from it was wrong.
  2. **bill B** — no box with a vendor suggestion, and no offer to create one.
     The explanation lived INSIDE the candidate div, whose
     `invisible="partner_id or not ai_vendor_candidate_ids"` hides it exactly
     when the candidate list is empty. A small contact book with next to no
     vendors in the sellers' country leaves the list legitimately empty every
     time, and the form said nothing at all.
  3. **bill C** — no one-button way to create the contact. It existed in no
     form: the module's own `_create_partner_from_extraction` says
     *"🔴 NEVER CREATE ONE"*.

🔴 WHAT THE BUTTON IS NOT. Automatic creation was removed in 19.0.3.0.0 on a
measurement — most sample bills minted a duplicate beside an active vendor —
and the source project's older version created vendors silently, which is
what is not wanted. A person pressing a button, having read the
chatter note listing what was on the document and which existing vendors come
closest, is a decision. The tests below pin both halves of that: it creates when
asked, it refuses a party that can never be a vendor, and pressing it twice
produces ONE contact.

⚠️ WHAT THESE TESTS CANNOT SEE. `get_view` returns the same arch whether a line
renders as one 31 px row or as two, so the two view changes here are asserted at
the level of *"the arch contains this, with this condition"* and no further.
**The browser look is outstanding and is flagged as such** — this file cannot
discharge the project's rule that a visual change is only done after it has been
looked at in a browser, light and dark.
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
    # 🔴 THE LESSON THAT COST TWO DEFECTS IN ONE DAY, BOTH FOUND IN A BROWSER
    # AFTER EVERY SERVER CHECK WAS GREEN.
    #
    # `get_view` returns ONE arch for every record in the table. So an
    # assertion of the form *"the arch contains this string"* cannot see a
    # modifier that is TOO PERMISSIVE: the element is present either way, and a
    # missing term in its `invisible=` reads as a pass. That is exactly how the
    # explanation block shipped visible on every hand-typed bill in the
    # database — `invisible="partner_id"` where it needed
    # `invisible="partner_id or not ai_from_import"`.
    #
    # The helpers below close that. `_element_invisible_for` pulls the real
    # `invisible` expression out of the arch and EVALUATES it against one
    # record's field values, which is what the web client does. So a test can
    # drive BOTH states — the record that should see the element and the record
    # that should not — and a too-permissive modifier fails instead of passing.
    #
    # ⚠️ WHAT IT STILL CANNOT SEE, stated rather than implied: pixels. Widths,
    # baselines, wrapping, colour and dark mode are outside any server test,
    # and this module's view comments are a list of things only a browser found
    # (a `<field>` measured at 504 px for a one-character integer;
    # `align-items-center` turning a count into a superscript). Where a view
    # change is visual, the browser look is a separate, outstanding step and
    # must be named as such.
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
        # The client evaluates the expression against the record's values. Only
        # the names the expression actually uses are read, so this does not
        # depend on knowing the whole form.
        names = {
            name
            for name in self.AM._fields
            if re.search(r"\b%s\b" % re.escape(name), expression)
        }
        values = {name: record[name] for name in names}
        # A many2one reads as its id, which is what the client sees and what
        # makes `invisible="partner_id"` truthy for a filed bill.
        for name, value in list(values.items()):
            if hasattr(value, "_name"):
                values[name] = value.id
        return bool(safe_eval(expression, values))


@tagged("post_install", "-at_install")
class TestTheCardNamesTheDraftCount(_UnmatchedHarness):
    """Report 1 — bill D."""

    def test_the_draft_count_is_reported_beside_the_posted_one(self):
        """🔴 RED. There was no draft number anywhere in the module.

        ⚠️ THE BILL ON SCREEN HAS TO BE AN IMPORTED ONE, and the first version
        of this test used a hand-made bill and read 0. That is not a bug: both
        counts are computed only for a bill with an `ai.invoice.import` row
        (`ai_from_import`), deliberately, because a bill typed by a person had
        its vendor chosen by that person and the line would be noise on every
        manual bill in the database. The field's own view note says so. So the
        bill under test is produced by `_run_import`, and the other drafts are
        hand-made because only the one on screen needs to be imported.
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
        """🔴 THE TRAP THE POSTED COUNT NEVER HAD.

        The old comment in `_compute_ai_vendor_bill_count` said a draft "is not
        in `total` to begin with", which was true while only posted rows were
        counted and false the moment drafts are. The bill on screen is almost
        always a draft, so without a per-state subtraction bill D would report
        "1 draft bill from this vendor" meaning itself.
        """
        move = self._run_import()
        move.action_ai_create_vendor_from_extraction()
        move.invalidate_recordset(["ai_vendor_draft_bill_count"])

        self.assertEqual(move.ai_vendor_draft_bill_count, 0)

    def test_control_a_vendor_with_no_bills_at_all_still_reads_zero_and_zero(self):
        """The control: the test above is not passing because every vendor has
        drafts. A brand-new vendor reports 0 posted and 0 draft, which is the
        state the plain zero-posted sentence is for."""
        vendor = self.Partner.create(
            {"name": "ZZ Brand New Vendor", "is_company": True}
        )

        counts = self.AM._ai_bill_counts(vendor, company=self.company)

        self.assertEqual(counts, {vendor: {}})
        self.assertEqual(self.AM._ai_posted_bill_counts(vendor), {vendor: 0})
        self.assertEqual(self.AM._ai_draft_bill_counts(vendor), {vendor: 0})

    def test_control_a_posted_bill_is_still_counted_as_posted(self):
        """The posted number is unchanged by the refactor — the whole reason
        `_ai_posted_bill_counts` was kept as a projection rather than replaced.

        Asserted on the HELPER, which is where the states are told apart; the
        move-level subtraction is the subject of the two tests above.
        """
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
        """🔴 THE COUNTING IS NOT FORKED. Both projections read
        `_ai_bill_counts`, so the card and the form line cannot drift into
        disagreeing about which rows they count. Stubbing the one body must
        change BOTH answers — the same shape of check as
        `test_vendor_matcher.TestOneBodyPerRule`.
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
        """The same too-permissive-modifier check on screen 1b.

        It has always carried `not ai_from_import`, so this is a lock rather
        than a repair — written because the block next door shipped without it
        and nothing noticed.
        """
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
    """Report 2 — bill B."""

    def test_the_explanation_is_not_inside_the_candidate_block(self):
        """🔴 RED. The sentence was inside a div hidden by
        `not ai_vendor_candidate_ids`, i.e. hidden in the one case it exists for.
        """
        arch = self._arch()

        self.assertIn("o_ai_vendor_unmatched", arch)
        self.assertIn(
            "This vendor was not recognised and nothing in the contact",
            arch,
            "the zero-candidate wording has to exist as its own string",
        )

    def test_the_explanation_is_hidden_on_a_bill_nobody_imported(self):
        """🔴 THE DEFECT THIS WHOLE PAIR EXISTS FOR, AND IT SHIPPED.

        Found in a browser, after every server test was green:
        pressing **New** on the Bills list showed *"This vendor was not
        recognised and nothing in the contact book came close… or create the
        contact from what was read off the document"* on a blank form. There is
        no document. The button was correctly hidden; the sentence was not,
        because the block had inherited only the `partner_id` half of the
        condition it used to live under.

        🔴 AND THE TEST THAT WAS HERE COULD NOT HAVE CAUGHT IT. It asserted
        that the arch CONTAINS the block, and the arch contains it either way —
        `get_view` returns one arch for every record. Only evaluating the
        modifier against a record that should not see the element shows a
        modifier that is too permissive. See the note on
        `_element_invisible_for`.
        """
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
        """The control, and without it the test above passes by hiding the block
        from everybody — which is the defect's mirror image and just as wrong."""
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
        """The end-to-end half: an import that matches nothing and finds no
        candidates still produces a bill the explanation applies to."""
        move = self._run_import()

        self.assertFalse(move.partner_id)
        self.assertFalse(
            move.ai_vendor_candidate_ids,
            "fixture check: nothing in this contact book resembles the seller, "
            "so this is the zero-candidate case that was reported",
        )
        self.assertTrue(
            move.ai_from_import and move.ai_can_create_vendor,
            "so the block and its button are both reachable",
        )

    def test_control_the_candidate_list_still_renders_when_there_are_candidates(self):
        """The control. Moving the sentence out must not take the list with it.

        Both states of the candidate block's OWN modifier, for the same reason
        as the test above: asserting it is present in the arch would pass
        whatever its condition said.
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
        """Both wordings sit under `invisible="partner_id"`, so a filed bill
        shows neither — otherwise the fix would have traded one wrong screen
        for another."""
        move = self._run_import()
        vendor = self.Partner.create({"name": "ZZ Picked", "is_company": True})

        move.partner_id = vendor

        self.assertFalse(
            move.ai_can_create_vendor,
            "and the button goes with it",
        )


@tagged("post_install", "-at_install")
class TestTheCreateContactButton(_UnmatchedHarness):
    """Report 3 — bill C."""

    def test_it_creates_one_contact_and_files_the_bill_against_it(self):
        """🔴 RED. No such action existed."""
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
            "F27's rule: a bank account is NEVER written automatically",
        )

    def test_it_learns_the_identifiers_so_the_next_bill_matches_itself(self):
        """The reason to create the contact rather than leave the bill empty."""
        move = self._run_import()

        move.action_ai_create_vendor_from_extraction()

        kinds = set(move.partner_id.identifier_ids.mapped("kind"))
        self.assertIn("phone", kinds)
        self.assertIn("domain", kinds)

    def test_a_second_bill_from_the_same_vendor_creates_nobody(self):
        """🔴 THE DUPLICATE RULE, AND THIS MODULE'S HISTORY IS WHY.

        ⚠️ AND THE SECOND BILL NEVER REACHES THE BUTTON, WHICH IS A BETTER
        ANSWER THAN THE ONE THIS TEST WAS WRITTEN TO ASSERT. The first version
        pressed the button on the second bill and failed with *"This bill
        already has a vendor"* — because the contact the first press created
        carries the phone and domain identifier rows `_ai_apply_vendor_data`
        added, so the IMPORT matched it by itself. The duplicate is prevented
        one step earlier than the button, by the matcher. That is the outcome
        the rule is about, so it is what is asserted.

        ⚠️ the core/source split: the second bill carries its OWN invoice
        number. The same number from the same vendor is now stopped as a
        duplicate invoice before any bill exists, so re-importing the first
        document would no longer reach the matcher this test is about.
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
        """The button's OWN dedupe branch, exercised directly.

        The gap it closes: the bill is imported with no match, somebody creates
        the contact by hand (or another bill's press creates it) and only THEN
        the button is pressed. `_ai_find_vendor` runs first, every time, so the
        press links rather than mints.
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
        """🔴 THE GUARD, FROM THE OTHER SIDE.

        A one-click create that wrote our own company — or minted a second copy
        of it — would reintroduce exactly the defect this branch fixes. The
        seller block here prints the company's own name, so the only thing it
        resolves to is a party that can never be a vendor.
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
        """A button's visibility rule is UI only; an RPC is not the UI."""
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
        """The control for the refusals above: `ai_can_create_vendor` is not
        simply always False."""
        move = self._run_import()

        self.assertTrue(move.ai_can_create_vendor)

    def test_the_arch_carries_the_button(self):
        arch = self._arch()

        self.assertIn("action_ai_create_vendor_from_extraction", arch)
        self.assertIn("Create contact from this invoice", arch)
        self.assertIn("ai_can_create_vendor", arch)

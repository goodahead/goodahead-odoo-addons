# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The bill updates the vendor record: empty fields are filled automatically,
conflicts are queued as suggestions for a person.

Every "nothing was written" assertion is paired with a control showing that
something else in the same extraction was written, so an apply that did
nothing cannot pass. `setUp` makes any call to the Claude extractor fail.
Fake `ZZ…` names, `555-01xx` numbers and `zz…` domains are used so nothing can
collide with real vendors in a copied database.
"""

import json
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_ai_import.lib import claude_client


def _extraction(**seller_overrides):
    """A minimal, arithmetically valid US extraction."""
    seller = {
        "name": "ZZ Unknown Trading Co",
        "vat": None,
        "business_id": None,
        "street": None,
        "city": None,
        "state_code": None,
        "postal_code": None,
        "country_code": "US",
        "phone": None,
        "email": None,
        "website": None,
    }
    seller.update(seller_overrides)
    return {
        "type": "Invoice",
        "invoice_number": "ZZ-VS-2",
        "issue_date": "2026-10-06",
        "currency": "USD",
        "seller": seller,
        "buyer": {"name": "ZZ Buyer", "country_code": "US"},
        "lines": [
            {
                "name": "a line",
                "quantity": 1.0,
                "unit_code": "C62",
                "price_unit": 10.0,
                "line_total": 10.0,
                "tax_percent": 0.0,
                "tax_amount": 0.0,
                "tax_category": "Z",
            }
        ],
        "tax_subtotals": [
            {
                "tax_percent": 0.0,
                "taxable_amount": 10.0,
                "tax_amount": 0.0,
                "tax_category": "Z",
            }
        ],
        "totals": {
            "line_extension": 10.0,
            "tax_exclusive": 10.0,
            "tax_inclusive": 10.0,
            "payable": 10.0,
        },
    }


class _SuggestionCase(TransactionCase):
    """The Claude guard, a purchase journal and the two models under test."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Partner = cls.env["res.partner"]
        cls.AM = cls.env["account.move"]
        cls.Suggestion = cls.env["goodahead.vendor.data.suggestion"]
        cls.Candidate = cls.env["goodahead.vendor.candidate"]
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", cls.env.company.id)],
            limit=1,
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

    # -- helpers -------------------------------------------------------

    def _bill(self):
        return self.AM.create(
            {"move_type": "in_invoice", "journal_id": self.journal.id}
        )

    def _apply(self, partner, extracted, move=None):
        """Run the apply against a bare bill, without the UBL decoder."""
        move = move or self._bill()
        return move, move._ai_apply_vendor_data(partner, extracted)

    def _import(self, extracted):
        """The whole path a real bill takes: UBL, decode, post-process."""
        xml_bytes = self.AM._ai_generate_ubl_from_extracted(
            extracted, self.env.company
        )
        move = self._bill()
        error = self.AM._ai_decode_ubl_into_move(move, xml_bytes, extracted)
        self.assertFalse(error, error)
        self.AM._ai_post_process(
            move,
            extracted,
            {"input_tokens": 0, "output_tokens": 0},
            "zz-test-model",
            self.env.company,
        )
        return move

    def _pending(self, move):
        return move.ai_vendor_suggestion_ids.filtered(
            lambda row: row.state == "pending"
        )


@tagged("post_install", "-at_install")
class TestTheSafeHalf(_SuggestionCase):
    """Empty vendor fields are filled automatically; conflicting ones are not
    touched."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.vendor = cls.Partner.create(
            {"name": "ZZ Sample Manufacturing", "is_company": True}
        )

    def test_an_empty_street_is_filled_with_nobody_asked(self):
        """Filling an empty field cannot lose anything, so it needs no
        approval."""
        move, summary = self._apply(
            self.vendor, _extraction(street="1 ZZ Nowhere Road", city="Newark")
        )

        self.assertEqual(self.vendor.street, "1 ZZ Nowhere Road")
        self.assertEqual(self.vendor.city, "Newark")
        self.assertIn(
            ("Street", "1 ZZ Nowhere Road"),
            summary["filled"],
            "the summary and the write must agree — the note is built from it",
        )
        self.assertFalse(self._pending(move), "nothing here needed a decision")

    def test_a_street_that_differs_is_not_overwritten(self):
        """A differing value is queued, never overwritten: the invoice is not
        authoritative over what somebody typed."""
        self.vendor.street = "99 ZZ Curated Avenue"

        move, summary = self._apply(self.vendor, _extraction(street="1 ZZ Nowhere Road"))

        self.assertEqual(
            self.vendor.street,
            "99 ZZ Curated Avenue",
            "a value a person typed must survive an import",
        )
        row = self._pending(move).filtered(lambda r: r.field_name == "street")
        self.assertEqual(len(row), 1, "the difference must be queued, not dropped")
        self.assertEqual(row.invoice_value, "1 ZZ Nowhere Road")
        self.assertEqual(row.record_value, "99 ZZ Curated Avenue")
        self.assertFalse(row.is_sensitive)

    def test_the_same_phone_printed_differently_is_not_a_conflict(self):
        """The same phone number in another notation is not a conflict."""
        self.vendor.phone = "+1 718-555-0111"

        move, _summary = self._apply(self.vendor, _extraction(phone="718.555.0111"))

        self.assertFalse(
            self._pending(move).filtered(lambda r: r.field_name == "phone"),
            "the same number in another notation is not a difference",
        )

    def test_a_genuinely_different_phone_is_a_conflict_that_keeps_both(self):
        """A different number is a conflict; applying it adds the number as an
        identifier and keeps the contact's phone."""
        self.vendor.phone = "+1 718-555-0111"

        move, _summary = self._apply(self.vendor, _extraction(phone="(973) 555-0144"))
        row = self._pending(move).filtered(lambda r: r.field_name == "phone")
        self.assertEqual(len(row), 1)

        row.action_apply()

        self.assertEqual(
            self.vendor.phone, "+1 718-555-0111", "the stored number is not replaced"
        )
        self.assertIn(
            ("phone", "9735550144"),
            [(r.kind, r.value) for r in self.vendor.identifier_ids],
            "it is kept as another number this vendor is known by",
        )

    def test_a_state_odoo_does_not_know_is_never_invented(self):
        """An unknown state code never creates a `res.country.state`; carriers
        reject bookings with invented states. The country in the same
        extraction is still applied."""
        State = self.env["res.country.state"]
        before = State.search_count([])

        move, summary = self._apply(
            self.vendor, _extraction(state_code="ZQ", country_code="US")
        )

        self.assertEqual(State.search_count([]), before, "no state may be created")
        self.assertFalse(self.vendor.state_id)
        self.assertEqual(self.vendor.country_id.code, "US", "the control")
        self.assertTrue(
            [row for row in summary["withheld"] if row[1] == "ZQ"],
            "the bill must say the state was not applied",
        )

    def test_the_country_is_resolved_before_the_state_needs_it(self):
        """The state lookup uses the country filled in by the same run."""
        move, summary = self._apply(
            self.vendor, _extraction(state_code="NJ", country_code="US")
        )

        self.assertEqual(self.vendor.country_id.code, "US")
        self.assertEqual(self.vendor.state_id.code, "NJ")
        self.assertFalse(summary["withheld"])


@tagged("post_install", "-at_install")
class TestWhatIsNeverAutomatic(_SuggestionCase):
    """Bank accounts and tax ids are never written automatically, empty or not.

    A changed bank account is the classic invoice-fraud vector. Scope: these
    tests cover `_ai_apply_vendor_data` only; core's UBL decoder creates the
    vendor's `res.partner.bank` itself (`account.edi.common._import_partner_bank`)
    before this code runs.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.vendor = cls.Partner.create(
            {"name": "ZZ Example Fleet Hire & Hauling", "is_company": True}
        )

    def test_a_bank_account_is_never_written_even_when_the_vendor_has_none(self):
        """The bank account is offered, not applied; an ordinary empty field in
        the same extraction is written."""
        move, summary = self._apply(
            self.vendor,
            _extraction(
                street="7 ZZ Depot Street",
                routing_number="021000021",
                account_number="ZZ5550001234",
            ),
        )

        self.assertFalse(self.vendor.bank_ids, "no bank account may be created")
        self.assertEqual(
            self.vendor.street, "7 ZZ Depot Street", "the control — this one IS written"
        )
        row = self._pending(move).filtered(lambda r: r.field_name == "bank_account")
        self.assertEqual(len(row), 1, "it is offered, not applied")
        self.assertTrue(row.is_sensitive, "and it is marked as a money detail")

    def test_a_tax_id_is_never_written_even_when_the_vendor_has_none(self):
        move, summary = self._apply(
            self.vendor, _extraction(street="7 ZZ Depot Street", vat="12-3456789")
        )

        self.assertFalse(self.vendor.vat, "a tax id waits for a person")
        self.assertEqual(self.vendor.street, "7 ZZ Depot Street", "the control")
        row = self._pending(move).filtered(lambda r: r.field_name == "vat")
        self.assertTrue(row.is_sensitive)

    def test_the_bulk_button_leaves_the_money_details_pending(self):
        """Bulk apply skips sensitive rows and applies the ordinary ones."""
        self.vendor.street = "99 ZZ Curated Avenue"
        move, _summary = self._apply(
            self.vendor,
            _extraction(
                street="7 ZZ Depot Street",
                vat="12-3456789",
                routing_number="021000021",
                account_number="ZZ5550001234",
            ),
        )
        rows = self._pending(move)
        self.assertEqual(len(rows), 3, "street, tax id, bank account")

        rows.action_apply_selected()

        states = {row.field_name: row.state for row in rows}
        self.assertEqual(states["street"], "applied", "the control")
        self.assertEqual(states["vat"], "pending")
        self.assertEqual(states["bank_account"], "pending")
        self.assertFalse(self.vendor.vat)
        self.assertFalse(self.vendor.bank_ids)

    def test_a_person_can_still_apply_one_money_detail_deliberately(self):
        """A person can still apply a sensitive row individually."""
        move, _summary = self._apply(
            self.vendor,
            _extraction(routing_number="021000021", account_number="ZZ5550001234"),
        )
        row = self._pending(move).filtered(lambda r: r.field_name == "bank_account")

        row.action_apply()

        self.assertEqual(row.state, "applied")
        self.assertEqual(self.vendor.bank_ids.acc_number, "ZZ5550001234")


@tagged("post_install", "-at_install")
class TestTheNameIsNeverTouched(_SuggestionCase):
    """The vendor name is curated on purpose and is never written or
    suggested."""

    def test_the_vendor_name_is_neither_written_nor_suggested(self):
        """The name is not offered while another field in the same extraction
        is."""
        vendor = self.Partner.create(
            {
                "name": "ZZACME - Smith reps",
                "is_company": True,
                "street": "99 ZZ Curated Avenue",
            }
        )

        move, _summary = self._apply(
            vendor, _extraction(name="ZZAcme", street="1 ZZ Factory Road")
        )

        self.assertEqual(vendor.name, "ZZACME - Smith reps")
        self.assertFalse(
            move.ai_vendor_suggestion_ids.filtered(lambda r: r.field_name == "name"),
            "the name must not even be offered",
        )
        self.assertTrue(
            self._pending(move).filtered(lambda r: r.field_name == "street"),
            "the control — something else in the same extraction was offered",
        )


@tagged("post_install", "-at_install")
class TestTheNearDuplicateNames(_SuggestionCase):
    """End to end: a manufacturer's bill against a rep agency in the ledger.

    No rule links the two. A person picks the candidate once, and the next bill
    from that vendor matches without anyone involved.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agency = cls.Partner.create(
            {
                "name": "ZZACME - Smith reps",
                "is_company": True,
                "supplier_rank": 86,
            }
        )

    # The vendor name here is a substring of the agency's name. Core's name
    # step must not match on a substring (the legacy core uses `ilike`), or
    # the bill is silently filed against the agency and the candidate flow
    # never runs. See also test_legacy_name_slot.
    def test_picking_a_candidate_once_teaches_the_matcher(self):
        first = self._import(
            _extraction(
                name="ZZAcme",
                website="https://www.zzacme-factory.com",
                street="1 ZZ Factory Road",
            )
        )

        # 1. nothing matched, nothing was invented, and the agency is offered.
        self.assertFalse(first.partner_id, "no vendor may be invented")
        candidates = first.ai_vendor_candidate_ids
        self.assertTrue(candidates, "the candidates must be ROWS, not just a note")
        self.assertIn(self.agency, candidates.partner_id)

        # 2. a person presses the button, once.
        row = candidates.filtered(lambda c: c.partner_id == self.agency)
        row.action_use_this()

        self.assertEqual(first.partner_id, self.agency)
        self.assertFalse(
            first.ai_vendor_candidate_ids,
            "the question has been answered; the alternatives go",
        )
        self.assertIn(
            ("domain", "zzacme-factory.com"),
            [(r.kind, r.value) for r in self.agency.identifier_ids],
            "the domain printed on the manufacturer's invoice is now the "
            "agency's — this is the learning",
        )
        self.assertEqual(
            self.agency.street,
            "1 ZZ Factory Road",
            "and the empty fields were filled while we were there",
        )

        # 3. the second bill, through the whole pipeline, with no human.
        second = self._import(
            _extraction(name="ZZAcme", website="https://www.zzacme-factory.com")
        )

        self.assertEqual(
            second.partner_id.commercial_partner_id,
            self.agency,
            "the second bill from the same vendor must match by itself — if "
            "this fails, picking a candidate taught the matcher nothing and "
            "the suggestion has no point",
        )
        self.assertFalse(
            second.ai_vendor_candidate_ids, "a matched bill offers no candidates"
        )

    def test_a_vendor_nobody_picks_teaches_nothing(self):
        """Without a pick, the second bill does not match."""
        self._import(
            _extraction(name="ZZAcme", website="https://www.zzacme-factory.com")
        )
        second = self._import(
            _extraction(name="ZZAcme", website="https://www.zzacme-factory.com")
        )

        self.assertFalse(
            second.partner_id,
            "without somebody pressing the button, nothing is learned",
        )

    def test_the_stored_extraction_is_found_again_and_not_stored_a_third_time(self):
        """`action_use_this` reads the stored extraction back; it is not copied
        onto the candidate row."""
        move = self._import(_extraction(name="ZZAcme", street="1 ZZ Factory Road"))

        found = move._ai_stored_extraction()

        self.assertEqual(found["invoice_number"], "ZZ-VS-2")
        self.assertNotIn(
            "extraction",
            self.Candidate._fields,
            "the extraction must not be copied onto the candidate row",
        )


@tagged("post_install", "-at_install")
class TestTheFormAndTheQueue(_SuggestionCase):
    """The XML parts, checked against the arch the server produces. This
    catches a silently dropped xpath, not visual layout."""

    def test_the_candidate_list_and_the_conflict_line_are_in_the_bill_form(self):
        """The inherited elements are present in the rendered bill form."""
        arch = self.AM.get_view(
            self.env.ref("account.view_move_form").id, "form"
        )["arch"]

        self.assertIn("ai_vendor_candidate_ids", arch)
        self.assertIn("action_use_this", arch)
        self.assertIn("action_ai_review_vendor_suggestions", arch)
        self.assertIn("action_ai_defer_vendor_suggestions", arch)
        self.assertIn("ai_vendor_suggestion_count", arch)

    def test_the_candidates_render_as_cards_and_not_as_a_cramped_grid(self):
        """Candidates render as a kanban, not a list (a list clipped the
        distinguishing part of long vendor names).

        The kanban class is also what the stylesheet rule hangs off.
        """
        from lxml import etree

        arch = etree.fromstring(
            self.AM.get_view(self.env.ref("account.view_move_form").id, "form")[
                "arch"
            ]
        )
        field = arch.xpath("//field[@name='ai_vendor_candidate_ids']")
        self.assertTrue(field, "the candidates are not in the form at all")
        kanban = field[0].xpath("./kanban")
        self.assertTrue(kanban, "the candidates must be cards, not a grid")
        self.assertFalse(
            field[0].xpath("./list"), "the cramped list must not come back"
        )
        self.assertIn(
            "o_ai_vendor_candidate_cards", kanban[0].get("class", "")
        )
        self.assertEqual(
            kanban[0].get("can_open"),
            "false",
            "a card click would open a form this model has no view for",
        )
        self.assertEqual(kanban[0].get("create"), "false")

    def test_the_conflict_line_counts_only_pending_rows(self):
        """The suggestion count includes only pending rows."""
        vendor = self.Partner.create(
            {
                "name": "ZZ Counting Probe",
                "is_company": True,
                "street": "99 ZZ Curated Avenue",
            }
        )

        clean, _summary = self._apply(vendor, _extraction(city=None, street=None))
        self.assertEqual(clean.ai_vendor_suggestion_count, 0, "the control")

        noisy, _summary = self._apply(vendor, _extraction(street="1 ZZ Nowhere Road"))
        self.assertEqual(noisy.ai_vendor_suggestion_count, 1)

        self._pending(noisy).action_dismiss()
        noisy.invalidate_recordset(["ai_vendor_suggestion_count"])
        self.assertEqual(
            noisy.ai_vendor_suggestion_count, 0, "a decided row stops asking"
        )

    def test_not_now_hides_the_line_and_leaves_the_rows_for_the_queue(self):
        """"Not now" defers the bill but leaves the rows pending for the
        queue."""
        vendor = self.Partner.create(
            {
                "name": "ZZ Defer Probe",
                "is_company": True,
                "street": "99 ZZ Curated Avenue",
            }
        )
        move, _summary = self._apply(vendor, _extraction(street="1 ZZ Nowhere Road"))

        move.action_ai_defer_vendor_suggestions()

        self.assertTrue(move.ai_vendor_review_deferred)
        self.assertEqual(
            self._pending(move).mapped("state"),
            ["pending"],
            "the row is handed on, not answered",
        )

    def test_every_search_default_names_a_filter_that_exists(self):
        """Every `search_default_*` in the action names an existing filter;
        unknown ones are dropped silently by the client.
        """
        from lxml import etree

        action = self.env.ref(
            "goodahead_invoice_ai_import.action_vendor_data_suggestion"
        )
        context = json.loads(action.context.replace("'", '"'))
        wanted = [
            key[len("search_default_"):]
            for key in context
            if key.startswith("search_default_")
        ]
        self.assertTrue(wanted, "the action is supposed to default-filter")

        arch = etree.fromstring(
            self.Suggestion.get_view(action.search_view_id.id, "search")["arch"]
        )
        names = arch.xpath("//filter/@name")
        for name in wanted:
            self.assertIn(
                name,
                names,
                "search_default_%s names no filter in the search view, so it "
                "will be dropped without a word" % name,
            )

    def test_the_review_button_opens_this_bills_pending_rows_in_a_dialog(self):
        """The review button opens a wizard over exactly this bill's pending
        rows.
        """
        vendor = self.Partner.create(
            {
                "name": "ZZ Review Probe",
                "is_company": True,
                "street": "99 ZZ Curated Avenue",
            }
        )
        move, _summary = self._apply(vendor, _extraction(street="1 ZZ Nowhere Road"))
        decided = self.Suggestion.create(
            {
                "move_id": move.id,
                "partner_id": vendor.id,
                "field_name": "city",
                "field_label": "City",
                "invoice_value": "ZZ Already Answered",
                "state": "dismissed",
            }
        )

        action = move.action_ai_review_vendor_suggestions()

        self.assertEqual(action["target"], "new", "a dialog, not a page away")
        self.assertEqual(
            action["res_model"], "goodahead.vendor.data.review"
        )
        wizard = self.env["goodahead.vendor.data.review"].browse(
            action["res_id"]
        )
        self.assertEqual(wizard.move_id, move)
        self.assertEqual(wizard.partner_id, vendor)
        self.assertEqual(
            wizard.line_ids.suggestion_id,
            self._pending(move),
            "the dialog shows the pending rows of this bill and nothing else",
        )
        self.assertNotIn(
            decided,
            wizard.line_ids.suggestion_id,
            "CONTROL: a row somebody already answered must not be asked again",
        )

    def test_nothing_sensitive_is_pre_ticked_and_everything_else_is(self):
        """Ordinary rows are pre-ticked in the wizard; sensitive rows never
        are.
        """
        vendor = self.Partner.create(
            {"name": "ZZ Pre-tick Probe", "is_company": True}
        )
        move = self._bill()
        safe = self.Suggestion.create(
            {
                "move_id": move.id,
                "partner_id": vendor.id,
                "field_name": "city",
                "field_label": "City",
                "invoice_value": "Wayne",
                "record_value": "Nanuet",
            }
        )
        risky = self.Suggestion.create(
            {
                "move_id": move.id,
                "partner_id": vendor.id,
                "field_name": "bank_account",
                "field_label": "Bank account",
                "invoice_value": "ZZ58231705",
                "is_sensitive": True,
            }
        )

        action = move.action_ai_review_vendor_suggestions()
        wizard = self.env["goodahead.vendor.data.review"].browse(
            action["res_id"]
        )

        ticked = wizard.line_ids.filtered("selected")
        self.assertEqual(
            ticked.suggestion_id, safe, "the safe row is offered ready to apply"
        )
        self.assertNotIn(
            risky,
            ticked.suggestion_id,
            "a bank account is never pre-ticked, empty field or not",
        )

    def test_apply_selected_writes_the_ticked_row_and_only_that_one(self):
        """The wizard applies the ticked row and leaves the unticked one
        pending."""
        vendor = self.Partner.create(
            {
                "name": "ZZ Wizard Apply Probe",
                "is_company": True,
                "street": "99 ZZ Curated Avenue",
                "city": "Nanuet",
            }
        )
        move, _summary = self._apply(
            vendor,
            _extraction(street="1 ZZ Nowhere Road", city="ZZ Wayne"),
        )
        action = move.action_ai_review_vendor_suggestions()
        wizard = self.env["goodahead.vendor.data.review"].browse(
            action["res_id"]
        )
        self.assertEqual(len(wizard.line_ids), 2, "two conflicts to decide")
        keep = wizard.line_ids.filtered(
            lambda line: line.suggestion_id.field_name == "city"
        )
        (wizard.line_ids - keep).selected = False

        wizard.action_apply_selected()

        self.assertEqual(vendor.city, "ZZ Wayne", "the ticked row was written")
        self.assertEqual(
            vendor.street,
            "99 ZZ Curated Avenue",
            "CONTROL: the unticked row left the curated value alone",
        )
        self.assertEqual(keep.suggestion_id.state, "applied")
        self.assertEqual(
            (wizard.line_ids - keep).suggestion_id.state,
            "pending",
            "an unticked row is not answered — it stays for the queue",
        )

    def test_not_now_in_the_wizard_defers_the_bill_and_keeps_the_rows(self):
        """The wizard's "Not now" defers the bill and keeps the rows
        pending."""
        vendor = self.Partner.create(
            {
                "name": "ZZ Wizard Defer Probe",
                "is_company": True,
                "street": "99 ZZ Curated Avenue",
            }
        )
        move, _summary = self._apply(vendor, _extraction(street="1 ZZ Nowhere Road"))
        action = move.action_ai_review_vendor_suggestions()
        wizard = self.env["goodahead.vendor.data.review"].browse(
            action["res_id"]
        )

        wizard.action_not_now()

        self.assertTrue(move.ai_vendor_review_deferred)
        self.assertEqual(self._pending(move).mapped("state"), ["pending"])

    def test_a_queue_row_opens_its_whole_vendor_not_the_single_row(self):
        """A queue row opens the review for all of that vendor's pending rows,
        and no other vendor's."""
        vendor = self.Partner.create(
            {"name": "ZZ Queue Click Probe", "is_company": True}
        )
        other = self.Partner.create(
            {"name": "ZZ Queue Click Control", "is_company": True}
        )
        move = self._bill()
        rows = self.Suggestion.create(
            [
                {
                    "move_id": move.id,
                    "partner_id": vendor.id,
                    "field_name": "city",
                    "field_label": "City",
                    "invoice_value": "Wayne",
                },
                {
                    "move_id": move.id,
                    "partner_id": vendor.id,
                    "field_name": "zip",
                    "field_label": "Zip",
                    "invoice_value": "07470",
                },
            ]
        )
        elsewhere = self.Suggestion.create(
            {
                "move_id": move.id,
                "partner_id": other.id,
                "field_name": "city",
                "field_label": "City",
                "invoice_value": "Newark",
            }
        )

        action = rows[0].action_open_review()
        wizard = self.env["goodahead.vendor.data.review"].browse(
            action["res_id"]
        )

        self.assertEqual(wizard.line_ids.suggestion_id, rows)
        self.assertFalse(
            wizard.move_id,
            "from the queue there is no one bill to defer, so none is claimed",
        )
        self.assertNotIn(
            elsewhere,
            wizard.line_ids.suggestion_id,
            "CONTROL: another vendor's row must not be dragged in",
        )

    def test_the_queue_row_click_and_the_wizard_buttons_name_real_methods(self):
        """The list row action and the wizard's footer buttons name existing
        methods; a wrong name only fails at click time.
        """
        from lxml import etree

        queue = etree.fromstring(
            self.Suggestion.get_view(
                self.env.ref(
                    "goodahead_invoice_ai_import."
                    "view_vendor_data_suggestion_list"
                ).id,
                "list",
            )["arch"]
        )
        row_click = queue.xpath("/list/@action")
        self.assertTrue(row_click, "a row click must open the review dialog")
        self.assertEqual(queue.xpath("/list/@type"), ["object"])
        self.assertIn(row_click[0], dir(self.Suggestion))

        Review = self.env["goodahead.vendor.data.review"]
        form = etree.fromstring(
            Review.get_view(
                self.env.ref(
                    "goodahead_invoice_ai_import.view_vendor_data_review_form"
                ).id,
                "form",
            )["arch"]
        )
        names = form.xpath("//footer/button[@type='object']/@name")
        self.assertTrue(names, "the dialog is supposed to have buttons")
        for name in names:
            self.assertIn(name, dir(Review), "%s is not a method" % name)

    def test_applying_a_state_suggestion_round_trips(self):
        """A state suggestion stores the code, which is what the apply looks
        up."""
        vendor = self.Partner.create(
            {"name": "ZZ State Round Trip", "is_company": True}
        )
        self._apply(vendor, _extraction(country_code="US", state_code="NJ"))
        self.assertEqual(vendor.state_id.code, "NJ")

        move, _summary = self._apply(
            vendor, _extraction(country_code="US", state_code="NY")
        )
        row = self._pending(move).filtered(lambda r: r.field_name == "state_id")
        self.assertEqual(row.invoice_value, "NY")

        row.action_apply()

        self.assertEqual(vendor.state_id.code, "NY")
        self.assertEqual(row.state, "applied")

    def test_a_state_suggestion_that_cannot_resolve_refuses_loudly(self):
        """Applying a state code that resolves to nothing raises instead of
        silently doing nothing."""
        vendor = self.Partner.create(
            {
                "name": "ZZ State Refusal",
                "is_company": True,
            }
        )
        row = self.Suggestion.create(
            {
                "move_id": self._bill().id,
                "partner_id": vendor.id,
                "field_name": "state_id",
                "field_label": "State",
                "invoice_value": "ZQ",
            }
        )

        with self.assertRaises(UserError):
            row.action_apply()

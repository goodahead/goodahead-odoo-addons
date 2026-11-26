# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""F27 part 2 — the bill teaches the vendor record, and the safe half of that
happens with nobody asked (19.0.3.0.0).

🔴 EVERY TEST HERE CARRIES A CONTROL. This project's recurring defect is a check
that reads a proxy and fails in the direction that looks like success, so
"nothing was written" is never asserted without "and this, in the same
extraction, was" — otherwise an apply that did nothing at all would pass the
whole safety half of this file.

🔴 NOTHING HERE MAY REACH THE CLAUDE API. `setUp` replaces
`ClaudeInvoiceExtractor.extract_invoice` with a function that raises, for every
test in every class below. ⚠️ The older `lib.claude_client.requests` patch named
in some notes no longer exists and must not be reintroduced: this module has
owned no HTTP transport since 19.0.1.2.0 — `goodahead_claude` does.

Fake `ZZ…` names, `555-01xx` numbers and `zz…` domains throughout. That is not
fussiness: part 1's tests first ran with Wide World's REAL phone number and FAILED,
because the real `Wide World Import` is in this database on that number and the
uniqueness rule correctly declined to choose between two vendors.
"""

import json
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_ai_import.lib import claude_client


def _extraction(**seller_overrides):
    """A minimal, arithmetically valid US extraction. Same shape as part 1's."""
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
        "invoice_number": "ZZ-F27-2",
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


class _F27Case(TransactionCase):
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
        """Run the apply against a bare bill, with no decoder in the way.

        Deliberate: this is the unit the rules belong to. The full pipeline is
        exercised by `TestTheNorthwindStory` below, where it is the pipeline that
        is under test.
        """
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
class TestTheSafeHalf(_F27Case):
    """A field that is EMPTY on the vendor is filled from the invoice with
    nobody asked. A field that CONTRADICTS it is not touched."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.vendor = cls.Partner.create(
            {"name": "ZZ Bluebird Manufacturing", "is_company": True}
        )

    def test_an_empty_street_is_filled_with_nobody_asked(self):
        """🔴 The rule: stop asking permission for the safe half. Filling an
        EMPTY field cannot lose anything."""
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
        """🔴 THE CONTROL for the test above, and the whole risk of this
        feature. An apply that simply wrote everything would pass that test and
        would quietly replace what somebody typed. ⚠️ The invoice is NOT
        authoritative — Tailspin prints a phone that is not the one the ledger
        holds, and that is a department, not an error."""
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
        """🔴 WITHOUT THIS EVERY VENDOR WITH A PHONE IS A "CONFLICT". The
        record holds `+1 212-555-0111` and the Bluebird invoice prints
        `212.555.0111` — the same telephone, printed two ways. A line
        that says "1 detail differs" on every single bill is a line nobody
        reads."""
        self.vendor.phone = "+1 718-555-0111"

        move, _summary = self._apply(self.vendor, _extraction(phone="718.555.0111"))

        self.assertFalse(
            self._pending(move).filtered(lambda r: r.field_name == "phone"),
            "the same number in another notation is not a difference",
        )

    def test_a_genuinely_different_phone_is_a_conflict_that_keeps_both(self):
        """🔴 THE CONTROL for the test above — a normaliser that collapsed
        everything would hide a real second number. And note what applying it
        does NOT do: the contact keeps its phone, and the invoice's number is
        stored as another way the vendor is reached. F27: *"Phone, email and
        website add beside, they do not replace."*"""
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
        """🔴 An import that invents US "states" — say a SECOND "New Jersey"
        whose code is `1` — breaks shipping: the state code is what Odoo sends
        to a carrier, which refuses a booking over it.

        CONTROL: the country in the same extraction IS applied, so an apply that
        had simply skipped this vendor could not pass.
        """
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
        """A state code is only unique inside a country, so the lookup needs
        one — and on a vendor that has neither, the country it needs is the one
        this very run just filled in. Ordering bug if it reads the record."""
        move, summary = self._apply(
            self.vendor, _extraction(state_code="NJ", country_code="US")
        )

        self.assertEqual(self.vendor.country_id.code, "US")
        self.assertEqual(self.vendor.state_id.code, "NJ")
        self.assertFalse(summary["withheld"])


@tagged("post_install", "-at_install")
class TestWhatIsNeverAutomatic(_F27Case):
    """🔴 A BANK ACCOUNT AND A TAX ID, EMPTY OR NOT. Invoice fraud IS a changed
    bank account — a real-looking invoice from a real-looking vendor carrying
    somebody else's account — and vendor invoices often carry a printed
    warning about exactly that scam.

    ⚠️ SCOPE, SAID PLAINLY. These tests exercise `_ai_apply_vendor_data`, which
    is what this feature owns. CORE'S UBL DECODER CREATES THE VENDOR'S
    `res.partner.bank` ITSELF on an `in_invoice`
    (`account_edi_ubl_cii/models/account_edi_ubl.py:2837-2865`,
    `_find_or_create_bank_account`), one step before any of this runs, and part
    1's `_ai_record_routing_number` depends on that row existing. So the rule
    holds for everything F27 part 2 added and does NOT hold for the pipeline as
    a whole. That is an open question, not something these tests
    may paper over.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.vendor = cls.Partner.create(
            {"name": "ZZ Tailspin Truck Leasing & Rental", "is_company": True}
        )

    def test_a_bank_account_is_never_written_even_when_the_vendor_has_none(self):
        """CONTROL: an ordinary empty field in the SAME extraction IS written,
        so this cannot pass by doing nothing at all."""
        move, summary = self._apply(
            self.vendor,
            _extraction(
                street="7 ZZ Depot Street",
                routing_number="071000288",
                account_number="ZZ4521992212",
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
        """🔴 Select-all is ONE CLICK. A money detail should get somebody's
        individual attention rather than ride along in a batch of address
        corrections.

        CONTROL: the ordinary row in the same selection IS applied, so a button
        that refused everything would not pass.
        """
        self.vendor.street = "99 ZZ Curated Avenue"
        move, _summary = self._apply(
            self.vendor,
            _extraction(
                street="7 ZZ Depot Street",
                vat="12-3456789",
                routing_number="071000288",
                account_number="ZZ4521992212",
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
        """🔴 THE CONTROL for the rule itself. "Never automatic" must not become
        "never at all" — a data owner who checked the account against something
        other than this invoice has to be able to record it, or the queue is a
        list of things nobody can act on."""
        move, _summary = self._apply(
            self.vendor,
            _extraction(routing_number="071000288", account_number="ZZ4521992212"),
        )
        row = self._pending(move).filtered(lambda r: r.field_name == "bank_account")

        row.action_apply()

        self.assertEqual(row.state, "applied")
        self.assertEqual(self.vendor.bank_ids.acc_number, "ZZ4521992212")


@tagged("post_install", "-at_install")
class TestTheNameIsNeverTouched(_F27Case):
    """🔴 `NORTHWIND - Smith reps` against the `Northwind` the manufacturer prints on
    its own paper. The name is the one field somebody curated on purpose."""

    def test_the_vendor_name_is_neither_written_nor_suggested(self):
        """CONTROL: a field in the same extraction DOES produce a suggestion, so
        an apply that produced nothing at all could not pass."""
        vendor = self.Partner.create(
            {
                "name": "ZZNORTHWIND - Smith reps",
                "is_company": True,
                "street": "99 ZZ Curated Avenue",
            }
        )

        move, _summary = self._apply(
            vendor, _extraction(name="ZZNorthwind", street="1 ZZ Factory Road")
        )

        self.assertEqual(vendor.name, "ZZNORTHWIND - Smith reps")
        self.assertFalse(
            move.ai_vendor_suggestion_ids.filtered(lambda r: r.field_name == "name"),
            "the name must not even be offered",
        )
        self.assertTrue(
            self._pending(move).filtered(lambda r: r.field_name == "street"),
            "the control — something else in the same extraction was offered",
        )


@tagged("post_install", "-at_install")
class TestTheNorthwindStory(_F27Case):
    """🔴 THE WHOLE FEATURE, END TO END, AND THE MOST VALUABLE TEST IN THIS PR.

    The bill comes from the MANUFACTURER and the ledger holds the REP AGENCY
    that sells for them — different company, different phone, different domain.
    No rule derives one from the other. A person says so ONCE, and the next bill
    from that vendor matches with nobody involved.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.agency = cls.Partner.create(
            {
                "name": "ZZNORTHWIND - Smith reps",
                "is_company": True,
                "supplier_rank": 63,
            }
        )

    # 🔴 THE NEXT TWO WERE LEFT FAILING ON THE LEGACY BENCH, DELIBERATELY, AND
    # THEY WERE THE MOST VALUABLE FAILURES IN THE SUITE. 🆕 THEY PASS ON BOTH
    # BENCHES SINCE 19.0.3.8.0 — keep reading, because WHY they failed is the
    # whole reason they are worth having, and the comment that claimed they
    # were still red outlived the fix by one version (found in review).
    #
    # *(measured, before 19.0.3.8.0)* On the legacy build the first import filed
    # the bill against `ZZNORTHWIND - Smith reps` — the agency — and neither
    # assertion below could hold. Nothing was invented: core's own name step
    # MATCHED it. The two cores spell that step differently and nobody had
    # noticed:
    #
    #   * search-plan: `res.partner._import_retrieve_customer_from_name`
    #     searches `[('name', '=ilike', name)]` — an EXACT, case-insensitive
    #     match (`account/models/partner.py:1013` at `189df353`).
    #   * legacy: `res.partner._retrieve_partner_with_name` searches
    #     `[('name', 'ilike', name)]` — a SUBSTRING match
    #     (`account/models/partner.py:956` at `ca1f0aea`).
    #
    # So on the legacy build a printed vendor name that is merely a SUBSTRING
    # of an existing partner's name won, confidently — and because this
    # module's whole recovery path (the note, the candidate cards, the
    # learning) is gated on an EMPTY `partner_id`, a wrong match does not add a
    # row, it SILENCES everything. F27's promise was therefore not kept on that
    # build, and the dual-core design did not fix it: suppressing a CREATE cannot
    # suppress a MATCH.
    #
    # 🆕 19.0.3.8.0 narrowed the slot this module owns to the other build's
    # exact spelling, so the Northwind bill now arrives with no vendor and the
    # candidate cards on BOTH builds, and these two are green on both. They stay
    # UNGATED, which is the point: they are the end-to-end tripwire on that
    # narrowing, and gating them would be how the gap comes back unnoticed.
    #
    # `test_legacy_name_slot.test_core_s_own_name_step_files_the_bill_against_
    # the_wrong_vendor` asserts the same core behaviour deliberately, from the
    # other side, so the difference is on the record twice.
    def test_picking_a_candidate_once_teaches_the_matcher(self):
        first = self._import(
            _extraction(
                name="ZZNorthwind",
                website="https://www.zznorthwind-factory.test",
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
            ("domain", "zznorthwind-factory.test"),
            [(r.kind, r.value) for r in self.agency.identifier_ids],
            "the domain printed on the manufacturer's invoice is now the "
            "agency's — this is the learning",
        )
        self.assertEqual(
            self.agency.street,
            "1 ZZ Factory Road",
            "and the empty fields were filled while we were there",
        )

        # 3. THE SECOND BILL, through the whole pipeline, with no human.
        second = self._import(
            _extraction(name="ZZNorthwind", website="https://www.zznorthwind-factory.test")
        )

        self.assertEqual(
            second.partner_id.commercial_partner_id,
            self.agency,
            "the second bill from the same vendor must match by itself — if "
            "this fails, picking a candidate taught the matcher nothing and "
            "F27 part 2 has no point",
        )
        self.assertFalse(
            second.ai_vendor_candidate_ids, "a matched bill offers no candidates"
        )

    def test_a_vendor_nobody_picks_teaches_nothing(self):
        """🔴 THE CONTROL. If the second import above matched because of the
        NAME, or because anything at all was written at import time, this would
        match too — and it must not."""
        self._import(
            _extraction(name="ZZNorthwind", website="https://www.zznorthwind-factory.test")
        )
        second = self._import(
            _extraction(name="ZZNorthwind", website="https://www.zznorthwind-factory.test")
        )

        self.assertFalse(
            second.partner_id,
            "without somebody pressing the button, nothing is learned",
        )

    def test_the_stored_extraction_is_found_again_and_not_stored_a_third_time(self):
        """`action_use_this` reads the extraction back. F27: *"It needs no new
        stored data"* — so it must come from one of the two copies that already
        exist, not from a field on the candidate row."""
        move = self._import(_extraction(name="ZZNorthwind", street="1 ZZ Factory Road"))

        found = move._ai_stored_extraction()

        self.assertEqual(found["invoice_number"], "ZZ-F27-2")
        self.assertNotIn(
            "extraction",
            self.Candidate._fields,
            "the extraction must not be copied onto the candidate row",
        )


@tagged("post_install", "-at_install")
class TestTheFormAndTheQueue(_F27Case):
    """The parts that live in XML. ⚠️ READ-VERIFIED, NOT SEEN RENDERED — there
    was no browser. These assert the arch the server actually produces, which
    catches a silently-dropped xpath; they say nothing about how it looks."""

    def test_the_candidate_list_and_the_conflict_line_are_in_the_bill_form(self):
        """🔴 An inherit whose xpath stops matching is SILENT: the feature is
        simply absent and every gate stays green. `get_view` is what the client
        actually receives."""
        arch = self.AM.get_view(
            self.env.ref("account.view_move_form").id, "form"
        )["arch"]

        self.assertIn("ai_vendor_candidate_ids", arch)
        self.assertIn("action_use_this", arch)
        self.assertIn("action_ai_review_vendor_suggestions", arch)
        self.assertIn("action_ai_defer_vendor_suggestions", arch)
        self.assertIn("ai_vendor_suggestion_count", arch)

    def test_the_candidates_render_as_cards_and_not_as_a_cramped_grid(self):
        """🔴 WHY A SERVER TEST ASSERTS A VIEW TYPE AT ALL. The four-column
        `<list>` this replaced installed cleanly, passed every gate, and in a
        browser clipped the vendor to "NORTHWIND - Smith r…" — which is precisely
        the half of the name that tells the three Northwind candidates apart. A
        test cannot see the clipping; it CAN see somebody quietly putting the
        list back.

        It also pins the class the one stylesheet rule hangs off: core declares
        `--KanbanRecord-width` ON `.o_kanban_renderer`, so the override has to
        be a descendant selector from this class, and a rename here would leave
        the rule matching nothing with everything still green.
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
        """CONTROL: a clean import shows none. A count that read
        `len(ai_vendor_suggestion_ids)` would put a line on every bill that ever
        had one — including the ones already dealt with."""
        vendor = self.Partner.create(
            {
                "name": "ZZ Contoso Counting Probe",
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
        """🔴 "Not now" must NOT dismiss. The queue is filtered on `pending`, so
        dismissing here would answer the question it exists to ask — and the
        person entering a bill would have silently decided for the person who
        owns vendor data."""
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
        """🔴 AN UNKNOWN `search_default_X` IS SILENTLY DROPPED. No error, no
        log line — you get an ungrouped list and conclude the grouping "did not
        work". This reads the action's own context and the search view's own
        arch, so a typo in either cannot pass.
        """
        from lxml import etree

        action = self.env.ref(
            "goodahead_invoice_import.action_vendor_data_suggestion"
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
        """🔴 IT OPENS A WIZARD NOW, NOT THE QUEUE'S OWN LIST VIEW. A list
        inside a modal was not what the design called for — it asks for
        wizards and modals that read clearly. So the
        assertion moved from "the domain is right" to "a wizard was built over
        exactly this bill's pending rows" — a domain cannot be wrong in a way
        this catches, but a wizard built over the wrong rows can.
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
        """🔴 THE WHOLE PROTECTION IN THIS DIALOG IS ONE DEFAULT. F27 asks for a
        DELIBERATE ACT on a bank account or a tax id, and here that act is
        ticking a box nobody ticked for you. The control is the other half: if
        the wizard pre-ticked NOTHING the safety assertion would pass while the
        feature did nothing, which is this project's signature failure shape.
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
        """The wizard must not reimplement the apply, and it must not apply
        what nobody ticked. Both halves in one test, because either on its own
        passes while the other is broken."""
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
        """Same contract as the thin line's own "Not now": hand it on, do not
        answer it. A wizard that dismissed would decide on behalf of whoever
        owns vendor data."""
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
        """🔴 THE SCOPE IS THE FEATURE. The queue is grouped by vendor so one
        person can do a vendor at a time; a row click that opened one
        suggestion would put them back to one detail at a time, which is how
        the same correction arrives seven times."""
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
        """🔴 A `<list action="…">` OR A FOOTER BUTTON NAMING A METHOD THAT DOES
        NOT EXIST FAILS AT CLICK TIME, IN THE BROWSER, AND NOWHERE ELSE. The
        install is green, every gate is green, and the screen is dead. This is
        the same class of defect as a `search_default_` naming no filter, and it
        is checked the same way — against the arch the server really ships.
        """
        from lxml import etree

        queue = etree.fromstring(
            self.Suggestion.get_view(
                self.env.ref(
                    "goodahead_invoice_import."
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
                    "goodahead_invoice_import.view_vendor_data_review_form"
                ).id,
                "form",
            )["arch"]
        )
        names = form.xpath("//footer/button[@type='object']/@name")
        self.assertTrue(names, "the dialog is supposed to have buttons")
        for name in names:
            self.assertIn(name, dir(Review), "%s is not a method" % name)

    def test_applying_a_state_suggestion_round_trips(self):
        """🔴 THE ROW HAS TO HOLD WHAT THE APPLY CONSUMES. Storing the display
        name ("New Jersey (US)") would read perfectly in the queue and then
        refuse to apply, because the lookup is by CODE."""
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
        """CONTROL for the round trip: the apply must not silently do nothing
        when the value resolves to no record. An invented state is what a silent write
        produces; a silent no-op is how you find out a year later that the queue
        never did anything."""
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

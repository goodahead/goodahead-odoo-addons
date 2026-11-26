# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The source interface and the matching policy (core/source split).

contracts/public-api.md §1 (`_create_for_source`, `_import_from_invoice_data`,
`ImportResult`), §1.3 (`MatchingPolicy`), ADR-0010; spec AC-04, AC-04b, AC-05,
AC-21. A tiny structured source lives in this file: it hands the core a
hand-built invoice data dict, with no Finvoice and no AI code anywhere.

Invented data only — every vendor is a `ZZ …` company with made-up numbers.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_import.lib.import_source import (
    DEFAULT_POLICY,
    STRICT_POLICY,
    ImportResult,
    MatchingPolicy,
)

from .common import ensure_purchase_journal


class _StructuredSource:
    """The smallest possible non-AI source: a document set arrives, it is
    recorded, and the invoice data it carries is imported — the shape the
    Finvoice source will have, without any Finvoice."""

    def __init__(self, env, policy=STRICT_POLICY):
        self.env = env
        self.policy = policy

    def receive(self, company, invoice_data, files=None):
        record = self.env["goodahead.invoice.import"]._create_for_source(
            "upload",
            company,
            files if files is not None else [("zz-invoice.xml", b"<zz/>", "application/xml")],
        )
        return record, record._import_from_invoice_data(invoice_data, self.policy)


@tagged("post_install", "-at_install")
class TestSourceInterface(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.Partner = cls.env["res.partner"].with_context(active_test=False)
        cls.Import = cls.env["goodahead.invoice.import"]
        cls.fi = cls.env.ref("base.fi")
        cls.journal = ensure_purchase_journal(cls.env, cls.company)
        if not cls.env["account.tax"].search(
            [
                ("type_tax_use", "=", "purchase"),
                ("amount_type", "=", "percent"),
                ("amount", "=", 25.5),
                ("company_id", "=", cls.company.id),
            ],
            limit=1,
        ):
            cls.env["account.tax"].create(
                {
                    "name": "ZZ T4 25.5% Purchase",
                    "type_tax_use": "purchase",
                    "amount_type": "percent",
                    "amount": 25.5,
                    "company_id": cls.company.id,
                }
            )

    # -- helpers ---------------------------------------------------------------

    def _data(self, number="ZZ-T4-1", **seller):
        seller_block = {
            "name": "ZZ Unknown Seller Oy",
            "vat": "FI90000001",
            "business_id": "9000000-1",
            "country_code": "FI",
            "phone": None,
            "email": None,
            "website": None,
        }
        seller_block.update(seller)
        return {
            "type": "Invoice",
            "invoice_number": number,
            "issue_date": "2026-11-02",
            "due_date": "2026-11-30",
            "currency": "EUR",
            "seller": seller_block,
            "buyer": {"name": self.company.name, "country_code": "FI"},
            "lines": [
                {
                    "product_code": "ZZ-1",
                    "name": "ZZ consulting",
                    "quantity": 2.0,
                    "unit_code": "C62",
                    "price_unit": 50.0,
                    "line_total": 100.0,
                    "tax_percent": 25.5,
                    "tax_amount": 25.5,
                    "tax_category": "S",
                }
            ],
            "tax_subtotals": [
                {
                    "tax_percent": 25.5,
                    "taxable_amount": 100.0,
                    "tax_amount": 25.5,
                    "tax_category": "S",
                }
            ],
            "totals": {
                "line_extension": 100.0,
                "tax_exclusive": 100.0,
                "tax_inclusive": 125.5,
                "payable": 125.5,
            },
        }

    def _vendor(self, name, **vals):
        return self.Partner.create(
            {"name": name, "is_company": True, "country_id": self.fi.id, **vals}
        )

    def _posted_bills(self, partner, count):
        """`count` bills from `partner` counted as posted. Posting for real
        would drag in approval and lock rules that are not under test; the
        candidate ranking only reads the state column."""
        for index in range(count):
            bill = self.env["account.move"].create(
                {
                    "move_type": "in_invoice",
                    "partner_id": partner.id,
                    "journal_id": self.journal.id,
                    "invoice_date": "2026-10-01",
                    "ref": f"ZZ-OLD-{partner.id}-{index}",
                    "invoice_line_ids": [
                        (0, 0, {"name": "ZZ old", "quantity": 1, "price_unit": 1.0})
                    ],
                }
            )
            self.env.flush_all()
            self.env.cr.execute(
                "UPDATE account_move SET state = 'posted' WHERE id = %s", (bill.id,)
            )
        self.env.invalidate_all()

    def _import(self, data, policy=STRICT_POLICY, files=None):
        return _StructuredSource(self.env, policy).receive(self.company, data, files)

    def _chatter(self, move):
        return " ".join(move.message_ids.mapped("body"))

    # -- the value objects -----------------------------------------------------

    def test_policy_constants(self):
        self.assertTrue(DEFAULT_POLICY.is_legacy)
        self.assertFalse(STRICT_POLICY.is_legacy)
        self.assertEqual(STRICT_POLICY.assign_by, ("vat", "business_id"))
        self.assertEqual(STRICT_POLICY.propose_by, ("phone", "domain", "name"))
        self.assertEqual(STRICT_POLICY.conflict, "leave_empty")
        with self.assertRaises(ValueError):
            MatchingPolicy(assign_by=("iban",))
        with self.assertRaises(ValueError):
            MatchingPolicy(conflict="pick_one")

    # -- _create_for_source ------------------------------------------------------

    def test_create_for_source_records_waiting_with_files(self):
        record = self.Import._create_for_source(
            "upload",
            self.company,
            [
                ("zz-package.xml", b"<zz/>", "application/xml"),
                ("zz-image.pdf", b"%PDF-1.4 zz", "application/pdf"),
            ],
            values={"source_email": "zz@example.invalid"},
        )
        self.assertEqual(len(record), 1)
        self.assertEqual(record.state, "waiting")
        self.assertEqual(record.source, "upload")
        self.assertEqual(record.company_id, self.company)
        self.assertEqual(record.name, "zz-package.xml")
        self.assertEqual(record.source_email, "zz@example.invalid")
        attachments = self.env["ir.attachment"].search(
            [("res_model", "=", self.Import._name), ("res_id", "=", record.id)]
        )
        self.assertEqual(
            sorted(attachments.mapped("name")), ["zz-image.pdf", "zz-package.xml"]
        )
        self.assertEqual(record.pdf_attachment_id.name, "zz-image.pdf")
        self.assertFalse(record.queue_job_id, "nothing is enqueued by creating")

    # -- the no-PDF path and AC-21 ------------------------------------------------

    def test_no_pdf_structured_import_builds_the_bill(self):
        vendor = self._vendor("ZZ Known Vendor Oy", vat="FI90000001")
        record, result = self._import(self._data())
        self.assertIsInstance(result, ImportResult)
        self.assertEqual(result.outcome, "imported", record.error_message or "")
        self.assertFalse(record.pdf_attachment_id, "no PDF was ever needed")
        self.assertEqual(record.state, "imported")
        self.assertEqual(result.move, record.move_id)
        self.assertFalse(result.duplicate_of)
        move = result.move
        self.assertEqual(move.move_type, "in_invoice")
        self.assertEqual(move.partner_id, vendor, "assigned by the VAT number")
        self.assertAlmostEqual(move.amount_untaxed, 100.0, places=2)
        self.assertAlmostEqual(move.amount_total, 125.5, places=2)
        self.assertEqual(record.invoice_number_normalized, "ZZT41")
        self.assertEqual(record.seller_vat, "FI90000001")

    def test_invoicing_user_without_contact_rights_gets_suggestions_not_a_failure(self):
        """An invoicing-only user (no Contact Creation) imports a bill whose
        vendor has an empty phone: the bill is imported, the vendor is not
        written (the ACL means something), and the fill becomes a suggestion
        for someone who can apply it — the import does not fail."""
        vendor = self._vendor("ZZ Empty Phone Oy", vat="FI90000001")
        user = self.env["res.users"].create(
            {
                "name": "ZZ Invoicing Only",
                "login": "zz-invoicing-only@example.test",
                "company_id": self.company.id,
                "company_ids": [(6, 0, self.company.ids)],
                "group_ids": [
                    (6, 0, [self.env.ref("base.group_user").id,
                            self.env.ref("account.group_account_invoice").id])
                ],
            }
        )
        if user.has_group("base.group_partner_manager"):
            self.skipTest("on this build invoicing users can edit contacts")
        data = self._data(number="ZZ-T4-NOACL", phone="+358 40 000 0001")
        record = self.env["goodahead.invoice.import"].with_user(user)._create_for_source(
            "upload", self.company, [("zz-invoice.xml", b"<zz/>", "application/xml")]
        )
        result = record._import_from_invoice_data(data, STRICT_POLICY)
        self.assertEqual(result.outcome, "imported", record.error_message or "")
        self.assertEqual(record.state, "imported")
        self.assertFalse(vendor.phone, "the vendor is not written without contact rights")
        suggestion = self.env["goodahead.vendor.data.suggestion"].sudo().search(
            [("move_id", "=", result.move.id), ("field_name", "=", "phone")]
        )
        self.assertEqual(len(suggestion), 1)
        self.assertEqual(suggestion.invoice_value, "+358 40 000 0001")

    def test_ac21_no_ai_key_no_ai_usage_neutral_chatter(self):
        """AC-21: with no AI key configured the structured import still runs,
        records no AI usage and does not describe itself as an AI reading."""
        if "claude_api_key" in self.company._fields:
            self.company.sudo().claude_api_key = False
        self._vendor("ZZ Keyless Vendor Oy", vat="FI90000001")
        record, result = self._import(self._data(number="ZZ-T4-KEYLESS"))
        self.assertEqual(result.outcome, "imported", record.error_message or "")
        for name in ("extraction_model", "input_tokens", "output_tokens"):
            if name in record._fields:
                self.assertFalse(record[name], f"{name} must not be recorded")
        chatter = self._chatter(result.move)
        self.assertIn("Imported from Upload", chatter)
        self.assertNotIn("Imported via AI", chatter)
        self.assertNotIn("tokens", chatter.lower())
        self.assertNotIn("Claude", chatter)
        self.assertTrue(
            self.env["ir.attachment"].search_count(
                [
                    ("res_model", "=", "account.move"),
                    ("res_id", "=", result.move.id),
                    ("name", "=", "extraction.json"),
                ]
            ),
            "the invoice data is attached to the bill as for every import",
        )

    # -- ImportResult outcomes ---------------------------------------------------

    def test_invalid_data_fails_with_its_code_and_keeps_the_data(self):
        data = self._data()
        del data["invoice_number"]
        record, result = self._import(data)
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.code, "import.invalid_data")
        self.assertTrue(result.message)
        self.assertFalse(result.move)
        self.assertEqual(record.state, "failed")
        self.assertEqual(record.failure_kind, "data")
        self.assertIn("ZZ consulting", record.extraction_data or "")

    def test_totals_that_do_not_add_up_fail_as_totals_mismatch(self):
        data = self._data()
        data["totals"]["payable"] = 999.0
        data["totals"]["tax_inclusive"] = 999.0
        record, result = self._import(data)
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.code, "import.totals_mismatch", result.message)
        self.assertEqual(record.state, "failed")

    def test_duplicate_seam_stops_the_import(self):
        existing = self.env["account.move"].create(
            {"move_type": "in_invoice", "journal_id": self.journal.id}
        )
        Import = type(self.Import)
        with patch.object(
            Import, "_import_check_duplicate", return_value=existing
        ) as seam:
            record, result = self._import(self._data(number="ZZ-T4-DUP"))
        self.assertTrue(seam.called)
        self.assertEqual(result.outcome, "duplicate")
        self.assertEqual(result.duplicate_of, existing)
        self.assertFalse(result.move)
        self.assertEqual(record.state, "duplicate")
        self.assertEqual(record.duplicate_of_move_id, existing)
        self.assertFalse(record.move_id)

    def test_no_earlier_bill_is_no_duplicate_and_an_override_skips_the_rule(self):
        """The duplicate rule finds nothing when no bill has the number, and
        is not even asked once an Accountant has overridden the flag."""
        record = self.Import._create_for_source("upload", self.company, [])
        self.assertFalse(record._import_check_duplicate(self._data(), self.Partner))
        record.write({"duplicate_override": True})
        with patch.object(type(self.Import), "_import_check_duplicate") as seam:
            result = record._import_from_invoice_data(self._data(number="ZZ-T4-OVR"), STRICT_POLICY)
        self.assertFalse(seam.called)
        self.assertEqual(result.outcome, "imported", record.error_message or "")

    def test_a_refused_decode_leaves_no_bill_and_keeps_the_data(self):
        """`_import_create_and_decode` runs in one savepoint: a decoder
        refusal rolls back the bill and the generated document, and the
        record fails with its data kept."""
        Move = type(self.env["account.move"])
        bills_before = self.env["account.move"].search_count([])
        with patch.object(Move, "_ai_decode_ubl_into_move", return_value="ZZ refused"):
            record, result = self._import(self._data(number="ZZ-T4-DECODE"))
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.code, "import.decode_failed")
        self.assertIn("ZZ refused", record.error_message)
        self.assertEqual(self.env["account.move"].search_count([]), bills_before)
        self.assertFalse(record.move_id)
        self.assertFalse(record.xml_attachment_id)
        self.assertFalse(
            self.env["ir.attachment"].search(
                [("res_model", "=", record._name), ("res_id", "=", record.id),
                 ("mimetype", "=", "application/xml"), ("name", "!=", "zz-invoice.xml")]
            ),
            "the generated document went with the savepoint",
        )
        self.assertIn("ZZ consulting", record.extraction_data)

    def test_an_imported_record_is_not_imported_twice(self):
        self._vendor("ZZ Twice Vendor Oy", vat="FI90000001")
        record, first = self._import(self._data(number="ZZ-T4-TWICE"))
        self.assertEqual(first.outcome, "imported")
        again = record._import_from_invoice_data(self._data(number="ZZ-T4-TWICE"), STRICT_POLICY)
        self.assertEqual(again.outcome, "imported")
        self.assertEqual(again.move, first.move)

    # -- AC-04: an unknown seller --------------------------------------------------

    def test_ac04_unknown_seller_gets_no_vendor_and_at_most_three_candidates(self):
        weak = [
            self._vendor(f"ZZ Lookalike {index}", email=f"ap{index}@zz-lookalike.example")
            for index in range(4)
        ]
        self._posted_bills(weak[2], 2)
        before = self.Partner.search_count([])
        record, result = self._import(
            self._data(
                number="ZZ-T4-UNKNOWN",
                name="ZZ Brand New Oy",
                email="billing@zz-lookalike.example",
            )
        )
        self.assertEqual(result.outcome, "imported", record.error_message or "")
        move = result.move
        self.assertFalse(move.partner_id, "no vendor is set for an unknown seller")
        self.assertEqual(self.Partner.search_count([]), before, "no vendor created")
        offered = move.ai_vendor_candidate_ids.sorted("sequence").partner_id
        self.assertEqual(len(offered), 3, "weak candidates are capped at three")
        self.assertEqual(offered[0], weak[2], "ranked by posted bills")
        chatter = self._chatter(move)
        self.assertIn("ZZ Brand New Oy", chatter, "the seller details are listed")
        self.assertIn("FI90000001", chatter)

        move.action_ai_create_vendor_from_extraction()
        self.assertEqual(self.Partner.search_count([]), before + 1)
        self.assertEqual(move.partner_id.name, "ZZ Brand New Oy")
        self.assertEqual(move.partner_id.vat, "FI90000001")

    # -- AC-04b: weak identifiers only propose under the strict policy --------------

    def test_ac04b_phone_match_only_proposes_under_the_strict_policy(self):
        # The same printed string, so core's own exact phone step matches too:
        # the policy has to overrule the decoder, not only our own steps.
        vendor = self._vendor("ZZ Phone Vendor Oy", phone="+358401234567")
        record, result = self._import(
            self._data(number="ZZ-T4-PHONE", name="ZZ Other Name", phone="+358401234567")
        )
        self.assertEqual(result.outcome, "imported", record.error_message or "")
        self.assertFalse(result.move.partner_id, "a phone match never sets the vendor")
        self.assertIn(vendor, result.move.ai_vendor_candidate_ids.partner_id)

    def test_ac04b_domain_and_name_matches_only_propose(self):
        by_domain = self._vendor("ZZ Domain Vendor Oy", email="sales@zz-domainvendor.example")
        by_name = self._vendor("ZZ Exact Name Oy")
        record, result = self._import(
            self._data(
                number="ZZ-T4-DOMAIN",
                name="ZZ Exact Name Oy",
                email="invoices@zz-domainvendor.example",
            )
        )
        self.assertEqual(result.outcome, "imported", record.error_message or "")
        self.assertFalse(result.move.partner_id)
        offered = result.move.ai_vendor_candidate_ids.partner_id
        self.assertIn(by_domain, offered)
        self.assertIn(by_name, offered)

    def test_ac04_strict_name_candidates_are_exact_names_only(self):
        """AC-04 "exact name": under the strict policy a vendor is offered by
        name only when its name IS the printed name (case aside); the AI's
        default list keeps its loose search."""
        exact = self._vendor("ZZ Northwind Oy")
        lookalike = self._vendor("ZZ Northwind Reps Oy")
        percent = self._vendor("ZZ 100 Percent Oy")
        Move = self.env["account.move"]
        seller = {"name": "zz NORTHWIND oy", "country_code": "FI"}
        strict = Move._ai_vendor_candidates(seller, company=self.company, policy=STRICT_POLICY)
        offered = {candidate["partner"] for candidate in strict}
        self.assertIn(exact, offered)
        self.assertNotIn(lookalike, offered)
        loose = Move._ai_vendor_candidates(seller, limit=20, company=self.company)
        self.assertIn(lookalike, {candidate["partner"] for candidate in loose})
        # A `%` in the printed name is a letter of the name, not a wildcard.
        strict = Move._ai_vendor_candidates(
            {"name": "ZZ 100% Oy", "country_code": "FI"}, company=self.company, policy=STRICT_POLICY
        )
        self.assertNotIn(percent, {candidate["partner"] for candidate in strict})

    def test_ac04b_default_policy_still_assigns_by_phone_and_domain(self):
        by_phone = self._vendor("ZZ Default Phone Oy", phone="+358409876543")
        record, result = self._import(
            self._data(number="ZZ-T4-DEF1", name="ZZ Something Else", phone="+358409876543"),
            policy=DEFAULT_POLICY,
        )
        self.assertEqual(result.outcome, "imported", record.error_message or "")
        self.assertEqual(result.move.partner_id, by_phone)

        by_domain = self._vendor("ZZ Default Domain Oy", email="ap@zz-defaultdomain.example")
        record, result = self._import(
            self._data(
                number="ZZ-T4-DEF2",
                name="ZZ Yet Another",
                vat="FI90000009",
                business_id="9000000-9",
                email="billing@zz-defaultdomain.example",
            ),
            policy=None,
        )
        self.assertEqual(result.outcome, "imported", record.error_message or "")
        self.assertEqual(result.move.partner_id, by_domain)

    def test_ac04b_vat_and_business_id_pointing_at_different_vendors(self):
        by_vat = self._vendor("ZZ Vat Holder Oy", vat="FI91111111")
        by_bid = self._vendor("ZZ Registry Holder Oy", company_registry="9222222-2")
        record, result = self._import(
            self._data(number="ZZ-T4-CONFLICT", vat="FI91111111", business_id="9222222-2")
        )
        self.assertEqual(result.outcome, "imported", record.error_message or "")
        move = result.move
        self.assertFalse(move.partner_id, "a VAT/Business ID conflict sets nobody")
        offered = move.ai_vendor_candidate_ids.partner_id
        self.assertEqual(set(offered.ids), {by_vat.id, by_bid.id})
        self.assertIn("point to different vendors", self._chatter(move))

    def test_ac04b_business_id_alone_assigns(self):
        vendor = self._vendor("ZZ Registry Only Oy", company_registry="9333333-3")
        record, result = self._import(
            self._data(number="ZZ-T4-BID", vat=None, business_id="9333333-3")
        )
        self.assertEqual(result.outcome, "imported", record.error_message or "")
        self.assertEqual(result.move.partner_id, vendor)

    # -- AC-05: several vendor companies share the identifier -----------------------

    def test_ac05_duplicate_vendor_companies_are_all_offered_by_bills(self):
        first = self._vendor("ZZ Dup Alpha Oy", vat="FI94444444")
        second = self._vendor("ZZ Dup Beta Oy", vat="FI94444444")
        third = self._vendor("ZZ Dup Gamma Oy", vat="FI94444444")
        # A contact under the first company carrying the same VAT is that
        # company, not a fourth vendor.
        self.Partner.create(
            {"name": "ZZ Dup Alpha contact", "parent_id": first.id, "vat": "FI94444444"}
        )
        self._posted_bills(third, 2)
        self._posted_bills(second, 1)
        record, result = self._import(self._data(number="ZZ-T4-AMBIG", vat="FI94444444"))
        self.assertEqual(result.outcome, "imported", record.error_message or "")
        move = result.move
        self.assertFalse(move.partner_id, "none of them is picked silently")
        offered = move.ai_vendor_candidate_ids.sorted("sequence").partner_id
        self.assertEqual(offered.ids, [third.id, second.id, first.id])
        self.assertIn("matches more than one vendor", self._chatter(move))

    def test_ac05_more_than_three_strong_candidates_are_not_capped(self):
        vendors = [self._vendor(f"ZZ Many {index} Oy", vat="FI95555555") for index in range(5)]
        record, result = self._import(self._data(number="ZZ-T4-MANY", vat="FI95555555"))
        self.assertEqual(result.outcome, "imported", record.error_message or "")
        self.assertFalse(result.move.partner_id)
        self.assertEqual(
            set(result.move.ai_vendor_candidate_ids.partner_id.ids),
            {vendor.id for vendor in vendors},
        )

    def test_ac05_contacts_of_one_company_count_as_one_vendor(self):
        company = self._vendor("ZZ Single Company Oy", vat="FI00000174")
        for index in range(3):
            self.Partner.create(
                {
                    "name": f"ZZ Single contact {index}",
                    "parent_id": company.id,
                    "vat": "FI00000174",
                }
            )
        record, result = self._import(self._data(number="ZZ-T4-CONTACTS", vat="FI00000174"))
        self.assertEqual(result.outcome, "imported", record.error_message or "")
        self.assertEqual(result.move.partner_id, company)

    # -- the decision itself, and the legacy pipeline's seam -------------------------

    def test_vendor_decision_under_both_policies(self):
        Partner = self.env["res.partner"]
        low = self._vendor("ZZ Decide Low Oy", vat="FI97777777", supplier_rank=1)
        high = self._vendor("ZZ Decide High Oy", vat="FI97777777", supplier_rank=9)
        seller = {"name": "ZZ Decide", "vat": "FI97777777", "country_code": "FI"}
        strict = Partner._ai_vendor_decision(seller, company=self.company, policy=STRICT_POLICY)
        self.assertFalse(strict["vendor"])
        self.assertEqual(strict["reason"], "ambiguous")
        self.assertEqual(set(strict["candidates"].ids), {low.id, high.id})
        default = Partner._ai_vendor_decision(seller, company=self.company)
        self.assertEqual(default["vendor"], high, "today's matcher: the established one")

    def test_legacy_pipeline_seam_applies_the_policy(self):
        """The older core's `_import_partner` (account.edi.common) must not set a
        vendor the policy forbids either — called directly, as its other tests
        do, so it runs on either core."""
        self._vendor("ZZ Legacy Phone Oy", phone="+358405550000")
        seller = {"name": "ZZ Legacy Other", "phone": "+358405550000", "country_code": "FI"}
        Common = self.env["account.edi.common"].with_context(
            ai_invoice_import=True,
            ai_seller=seller,
            goodahead_matching_policy=STRICT_POLICY,
        )
        partner, logs = Common._import_partner(
            self.company, "ZZ Legacy Other", "+358405550000", None, None
        )
        self.assertFalse(partner)
        self.assertEqual(logs, [])

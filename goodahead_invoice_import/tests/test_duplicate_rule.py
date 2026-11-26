# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The duplicate-invoice rule (core/source split): spec AC-22, ADR-0009, and the
company rule on the import record.

Invented data only — every vendor is a `ZZ …` company with made-up numbers.
"""

from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools import SQL

from odoo.addons.goodahead_invoice_import.lib.import_source import (
    DEFAULT_POLICY,
    STRICT_POLICY,
)

from .common import ensure_purchase_journal


@tagged("post_install", "-at_install")
class TestDuplicateRule(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.Partner = cls.env["res.partner"].with_context(active_test=False)
        cls.Import = cls.env["goodahead.invoice.import"]
        cls.Move = cls.env["account.move"]
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
                    "name": "ZZ T5 25.5% Purchase",
                    "type_tax_use": "purchase",
                    "amount_type": "percent",
                    "amount": 25.5,
                    "company_id": cls.company.id,
                }
            )
        cls.vendor = cls.Partner.create(
            {
                "name": "ZZ Duplicate Vendor Oy",
                "is_company": True,
                "country_id": cls.fi.id,
                "vat": "FI98000001",
            }
        )

    # -- helpers ---------------------------------------------------------------

    def _data(self, number="INV-001", **seller):
        seller_block = {
            "name": "ZZ Duplicate Vendor Oy",
            "vat": "FI98000001",
            "business_id": "9800000-1",
            "country_code": "FI",
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
                    "name": "ZZ service",
                    "quantity": 1.0,
                    "unit_code": "C62",
                    "price_unit": 100.0,
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

    def _bill(self, ref, partner=None, move_type="in_invoice", journal=None):
        """An existing bill, header only: the rule reads the company, kind,
        state, reference and vendor, nothing else."""
        journal = journal or self.journal
        return self.Move.with_company(journal.company_id).create(
            {
                "move_type": move_type,
                "partner_id": (partner or self.vendor).id,
                "journal_id": journal.id,
                "ref": ref,
            }
        )

    def _import(self, data, policy=STRICT_POLICY):
        record = self.Import._create_for_source(
            "upload", self.company, [("zz.xml", b"<zz/>", "application/xml")]
        )
        return record, record._import_from_invoice_data(data, policy)

    # -- AC-22 -------------------------------------------------------------------

    def test_same_vendor_same_number_ignoring_case_and_separators(self):
        existing = self._bill("INV-001")
        before = self.Move.search_count([("move_type", "=", "in_invoice")])
        record, result = self._import(self._data(number="inv 001"))
        self.assertEqual(result.outcome, "duplicate", record.error_message or "")
        self.assertEqual(result.code, "import.duplicate")
        self.assertEqual(result.duplicate_of, existing)
        self.assertFalse(result.move)
        self.assertEqual(record.state, "duplicate")
        self.assertEqual(record.duplicate_of_move_id, existing)
        self.assertFalse(record.move_id)
        self.assertEqual(
            self.Move.search_count([("move_type", "=", "in_invoice")]),
            before,
            "no second bill",
        )
        self.assertEqual(record.invoice_number_normalized, "INV001")
        self.assertTrue(record.extraction_data, "the data is kept for an override")
        note = " ".join(record.message_ids.mapped("body"))
        self.assertIn("duplicate", note)
        self.assertIn('data-oe-model="account.move"', note.replace("'", '"'))
        self.assertIn(f'data-oe-id="{existing.id}"', note.replace("'", '"'), "links the bill")
        self.assertNotIn(record.state, ("imported", "closed"), "stays unsettled")

    def test_default_policy_finds_the_duplicate_too(self):
        existing = self._bill("INV-001")
        record, result = self._import(self._data(number="INV001"), policy=DEFAULT_POLICY)
        self.assertEqual(result.outcome, "duplicate")
        self.assertEqual(result.duplicate_of, existing)

    def test_contact_of_the_vendor_counts_as_the_vendor(self):
        contact = self.Partner.create({"name": "ZZ AP desk", "parent_id": self.vendor.id})
        existing = self._bill("INV-001", partner=contact)
        record, result = self._import(self._data())
        self.assertEqual(result.outcome, "duplicate")
        self.assertEqual(result.duplicate_of, existing)

    def test_same_number_from_another_vendor_is_not_a_duplicate(self):
        other = self.Partner.create(
            {"name": "ZZ Other Vendor Oy", "is_company": True, "vat": "FI98000002"}
        )
        self._bill("INV-001", partner=other)
        record, result = self._import(self._data())
        self.assertEqual(result.outcome, "imported", record.error_message or "")
        self.assertTrue(record.move_id)

    def test_cancelled_bill_is_not_a_duplicate(self):
        existing = self._bill("INV-001")
        existing.button_cancel()
        self.assertEqual(existing.state, "cancel")
        record, result = self._import(self._data())
        self.assertEqual(result.outcome, "imported", record.error_message or "")

    def test_bill_in_another_company_is_not_a_duplicate(self):
        other_company = self.env["res.company"].create({"name": "ZZ Other Company"})
        other_journal = self.env["account.journal"].create(
            {
                "name": "ZZ Other Purchases",
                "code": "ZZOP",
                "type": "purchase",
                "company_id": other_company.id,
            }
        )
        self._bill("INV-001", journal=other_journal)
        record, result = self._import(self._data())
        self.assertEqual(result.outcome, "imported", record.error_message or "")

    def test_credit_note_and_invoice_with_the_same_number_are_kept_apart(self):
        self._bill("INV-001", move_type="in_refund")
        record, result = self._import(self._data())
        self.assertEqual(result.outcome, "imported", record.error_message or "")
        self.assertEqual(record.move_id.move_type, "in_invoice")

    def test_unmatched_vendor_same_seller_vat_is_a_duplicate(self):
        """Two vendor companies share the VAT number, so the strict policy
        matches nobody; the earlier bill's partner carries the seller's VAT."""
        twin = self.Partner.create(
            {"name": "ZZ Duplicate Vendor Twin Oy", "is_company": True, "vat": "FI98000001"}
        )
        decision = self.env["res.partner"]._ai_vendor_decision(
            self._data()["seller"], company=self.company, policy=STRICT_POLICY
        )
        self.assertFalse(decision["vendor"], "precondition: no vendor matched")
        existing = self._bill("INV-001", partner=twin)
        record, result = self._import(self._data())
        self.assertEqual(result.outcome, "duplicate", record.error_message or "")
        self.assertEqual(result.duplicate_of, existing)

    def test_unmatched_vendor_same_business_id_is_a_duplicate(self):
        archived = self.Partner.create(
            {
                "name": "ZZ Archived Registry Oy",
                "is_company": True,
                "company_registry": "0000048-1",
            }
        )
        existing = self._bill("ZZ-77", partner=archived)
        archived.active = False
        record, result = self._import(
            self._data(number="zz 77", vat=None, business_id="0000048-1", name="ZZ Nobody")
        )
        self.assertEqual(result.outcome, "duplicate", record.error_message or "")
        self.assertEqual(result.duplicate_of, existing)

    def test_earlier_import_record_counts_when_the_bill_ref_was_changed(self):
        first, result = self._import(self._data(number="ZZ-REF-9"))
        self.assertEqual(result.outcome, "imported", first.error_message or "")
        first.move_id.ref = "changed by hand"
        second, again = self._import(self._data(number="ZZ REF 9"))
        self.assertEqual(again.outcome, "duplicate")
        self.assertEqual(again.duplicate_of, first.move_id)

    def test_override_creates_the_bill(self):
        existing = self._bill("INV-001")
        record, result = self._import(self._data())
        self.assertEqual(result.outcome, "duplicate")
        # No structured reader claims this hand-made record, so the override
        # imports the stored data at once (see the two override tests below).
        record.action_override_duplicate()
        self.assertTrue(record.duplicate_override)
        self.assertEqual(record.state, "imported", record.error_message or "")
        self.assertTrue(record.move_id)
        self.assertNotEqual(record.move_id, existing)

    def test_close_settles_a_duplicate(self):
        self._bill("INV-001")
        record, result = self._import(self._data())
        self.assertEqual(result.outcome, "duplicate")
        record.action_close_without_bill("ZZ paper copy of the e-invoice")
        self.assertEqual(record.state, "closed")
        self.assertFalse(record.move_id)

    def test_ai_door_is_checked_too(self):
        """`_finalize_import` — the door an AI reading (and the re-import from
        stored data) comes through — stops a duplicate before any bill."""
        existing = self._bill("INV-001")
        data = self._data(number="Inv.001")
        record = self.Import.create({"name": "zz.pdf", "company_id": self.company.id})
        before = self.Move.search_count([])
        detail = record._finalize_import(
            {
                "extracted": data,
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "model_name": "zz-model",
                "xml_bytes": self.Move._ai_generate_ubl_from_extracted(data, self.company),
            }
        )
        self.assertTrue(detail.startswith("DUPLICATE"), detail)
        self.assertEqual(record.state, "duplicate")
        self.assertEqual(record.duplicate_of_move_id, existing)
        self.assertEqual(self.Move.search_count([]), before)
        self.assertTrue(record.extraction_data, "the paid reading is kept")

    def test_override_of_a_read_record_imports_from_the_stored_data(self):
        """A duplicate stopped AFTER a paid reading is imported from the data
        that reading stored: no job, no second reading."""
        existing = self._bill("INV-001")
        data = self._data(number="Inv.001")
        record = self.Import.create({"name": "zz.pdf", "company_id": self.company.id})
        record._finalize_import(
            {
                "extracted": data,
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "model_name": "zz-model",
                "xml_bytes": self.Move._ai_generate_ubl_from_extracted(data, self.company),
            }
        )
        self.assertEqual(record.state, "duplicate")
        Model = type(self.Import)
        with (
            patch.object(Model, "_enqueue_import", side_effect=AssertionError("read again")),
            patch.object(Model, "_source_read", side_effect=AssertionError("read again")),
        ):
            record.action_override_duplicate()
        self.assertEqual(record.state, "imported", record.error_message or "")
        self.assertTrue(record.duplicate_override)
        self.assertTrue(record.move_id)
        self.assertNotEqual(record.move_id, existing)
        self.assertFalse(record.queue_job_id, "nothing was enqueued")

    def test_override_of_a_structured_record_reads_it_again(self):
        """A structured source's record is enqueued: reading it costs
        nothing, and the source may have work of its own in the job."""
        self._bill("INV-001")
        record, result = self._import(self._data())
        self.assertEqual(result.outcome, "duplicate")
        Model = type(self.Import)
        with (
            patch.object(Model, "_import_reader_spec",
                         return_value=type("Spec", (), {"structured": True})()),
            patch.object(Model, "_enqueue_import") as enqueue,
        ):
            record.action_override_duplicate()
        enqueue.assert_called_once()
        self.assertFalse(record.move_id)

    def test_spaced_vat_on_the_partner_matches_the_invoices_vat(self):
        """No vendor matched: the earlier bill's partner is found by VAT,
        compared without spaces and case on both sides."""
        spaced = self.Partner.create(
            {"name": "ZZ Spaced VAT Oy", "is_company": True, "vat": "fi 9800 0003"}
        )
        existing = self._bill("ZZ-SP-1", partner=spaced)
        record = self.Import.create({"name": "zz.pdf", "company_id": self.company.id})
        found = record._import_check_duplicate(
            self._data(number="ZZ SP 1", vat="FI98000003", business_id=None),
            self.Partner.browse(),
        )
        self.assertEqual(found, existing)

    def test_seller_vat_is_stored_normalised_and_finds_the_earlier_record(self):
        first, result = self._import(
            self._data(number="ZZ-NV-1", vat="fi 9800 0001")
        )
        self.assertEqual(result.outcome, "imported", first.error_message or "")
        self.assertEqual(first.seller_vat, "FI98000001")
        first.move_id.ref = "changed by hand"
        second = self.Import.create({"name": "zz.pdf", "company_id": self.company.id})
        found = second._import_duplicate_earlier_record(
            "ZZNV1", "in_invoice", self.Partner.browse(), "FI98000001", []
        )
        self.assertEqual(found, first.move_id)

    def test_lookup_uses_the_bill_ref_index(self):
        """The SQL spells the reference expression exactly as the index, so
        PostgreSQL can use it (forced here: on a test database a sequential
        scan of a few rows is cheaper and would be chosen)."""
        record = self.Import.create({"name": "zz.pdf", "company_id": self.company.id})
        for vendor, vat, registry in (
            (self.vendor, "", []),
            (self.Partner.browse(), "FI98000001", ["9800000-1"]),
        ):
            query = record._import_duplicate_bill_query(
                "INV001", "in_invoice", vendor, vat, registry
            )
            self.env.cr.execute("SAVEPOINT zz_explain")
            self.env.cr.execute("SET LOCAL enable_seqscan = off")
            self.env.cr.execute(SQL("EXPLAIN %s", query))
            plan = "\n".join(row[0] for row in self.env.cr.fetchall())
            self.env.cr.execute("ROLLBACK TO SAVEPOINT zz_explain")
            self.assertIn("goodahead_invoice_import_move_ref_norm_idx", plan, plan)


@tagged("post_install", "-at_install")
class TestImportCompanyRule(TransactionCase):
    """AC-15: a user of company A cannot read company B's import records."""

    def test_user_of_one_company_cannot_read_another_companys_imports(self):
        company_a = self.env.company
        company_b = self.env["res.company"].create({"name": "ZZ Company B"})
        Import = self.env["goodahead.invoice.import"]
        own = Import.create({"name": "zz-a.pdf", "company_id": company_a.id})
        foreign = Import.create({"name": "zz-b.pdf", "company_id": company_b.id})
        user = new_test_user(
            self.env,
            login="zz_t5_company_a",
            groups="account.group_account_user,account.group_account_invoice",
            company_id=company_a.id,
            company_ids=[(6, 0, [company_a.id])],
        )
        as_user = Import.with_user(user)
        visible = as_user.search([("id", "in", (own | foreign).ids)])
        self.assertEqual(visible, own)
        with self.assertRaises(AccessError):
            foreign.with_user(user).read(["name"])
        self.assertTrue(
            self.env.ref("goodahead_invoice_import.rule_goodahead_invoice_import_company")
        )

# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""End-to-end import test.

Only the paid Claude call (``_ai_extract_and_generate_ubl``) is mocked; generation,
decoding and post-processing run for real, so the assertions are on the vendor bill
Odoo actually builds. Fake ``ZZ-…`` identifiers avoid matching existing partners.
"""

from unittest.mock import MagicMock, patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_ai_import.lib.ubl_generator import (
    generate_peppol_invoice_xml,
)

NOTE = "Viivastyskorko 16 %. Huomautusaika 7 pv."


@tagged("post_install", "-at_install")
class TestImportEndToEnd(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.company.claude_api_key = "test-key"
        cls.fi = cls.env.ref("base.fi")
        cls.Partner = cls.env["res.partner"]

        # The UBL decoder needs a 25.5% purchase tax to map the line's TaxCategory.
        cls.tax = cls.env["account.tax"].search(
            [
                ("type_tax_use", "=", "purchase"),
                ("amount_type", "=", "percent"),
                ("amount", "=", 25.5),
                ("company_id", "=", cls.company.id),
            ],
            limit=1,
        )
        if not cls.tax:
            cls.tax = cls.env["account.tax"].create(
                {
                    "name": "ZZ Test 25.5% Purchase",
                    "type_tax_use": "purchase",
                    "amount_type": "percent",
                    "amount": 25.5,
                    "company_id": cls.company.id,
                }
            )

        # Existing vendor the import must reuse (business id + country).
        cls.vendor = cls.Partner.create(
            {
                "name": "ZZ E2E Vendor Oy",
                "is_company": True,
                "company_registry": "ZZ-E2E-0001",
                "country_id": cls.fi.id,
            }
        )

    def _extracted(self):
        """A discounted line: list 38.69, net 20.95, 25.5% VAT 5.34, total 26.29."""
        return {
            "type": "Invoice",
            "invoice_number": "E2E-1",
            "issue_date": "2026-08-06",
            "currency": "EUR",
            "note": NOTE,
            "seller": {
                "name": "ZZ E2E Vendor Oy",
                "vat": "FI09990001",
                "business_id": "ZZ-E2E-0001",
                "country_code": "FI",
            },
            "buyer": {
                "name": self.company.name,
                "vat": self.company.vat or "FI20000002",
                "country_code": "FI",
            },
            "lines": [
                {
                    "product_code": "1000001",
                    "name": "WC-ISTUINKANSI",
                    "quantity": 1.0,
                    "unit_code": "C62",
                    "price_unit": 38.69,
                    "line_total": 20.95,
                    "tax_percent": 25.5,
                    "tax_amount": 5.34,
                    "tax_category": "S",
                }
            ],
            "tax_subtotals": [
                {
                    "tax_percent": 25.5,
                    "taxable_amount": 20.95,
                    "tax_amount": 5.34,
                    "tax_category": "S",
                }
            ],
            "totals": {
                "line_extension": 20.95,
                "tax_exclusive": 20.95,
                "tax_inclusive": 26.29,
                "payable": 26.29,
            },
        }

    def _new_import(self, **vals):
        att = self.env["ir.attachment"].create(
            {
                "name": "e2e.pdf",
                "raw": b"%PDF-1.4 fake",
                "mimetype": "application/pdf",
            }
        )
        base = {
            "name": "e2e.pdf",
            "company_id": self.company.id,
            "pdf_attachment_id": att.id,
        }
        base.update(vals)
        return self.env["ai.invoice.import"].create(base)

    def _ai_result(self, extracted, xml_bytes):
        return {
            "extracted": extracted,
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "model_name": "test-model",
            "xml_bytes": xml_bytes,
        }

    def _patch_extract(self, ai_result):
        return patch.object(
            type(self.env["account.move"]),
            "_ai_extract_and_generate_ubl",
            return_value=ai_result,
        )

    def test_full_import_builds_correct_discounted_bill(self):
        """One line at the net price, no 'Rounding' line, correct totals, the note
        once, the PDF attached and the existing vendor reused."""
        extracted = self._extracted()
        xml_bytes = generate_peppol_invoice_xml(extracted)
        rec = self._new_import()
        with self._patch_extract(self._ai_result(extracted, xml_bytes)):
            result = rec._process_import()

        self.assertTrue(result.startswith("OK"), result)
        self.assertEqual(rec.state, "done")
        move = rec.move_id
        self.assertTrue(move, "a vendor bill must be created and linked")

        product_lines = move.invoice_line_ids.filtered(
            lambda ln: ln.display_type not in ("line_section", "line_note")
        )
        self.assertEqual(
            len(product_lines), 1, "exactly one line — no bogus 'Rounding' line"
        )
        self.assertFalse(
            any("round" in (ln.name or "").lower() for ln in move.invoice_line_ids),
            "no 'Rounding' line must be added",
        )
        self.assertAlmostEqual(
            product_lines.price_unit, 20.95, places=2,
            msg="net (discounted) unit price, not the 38.69 list price",
        )
        self.assertAlmostEqual(move.amount_untaxed, 20.95, places=2)
        self.assertAlmostEqual(move.amount_tax, 5.34, places=2)
        self.assertAlmostEqual(move.amount_total, 26.29, places=2)

        self.assertEqual(
            move.partner_id, self.vendor, "existing vendor reused, not duplicated"
        )

        pdfs = self.env["ir.attachment"].search(
            [
                ("res_model", "=", "account.move"),
                ("res_id", "=", move.id),
                ("mimetype", "=", "application/pdf"),
            ]
        )
        self.assertTrue(pdfs, "the original PDF must be attached to the bill")

        self.assertEqual(
            (move.narration or "").count(NOTE), 1,
            "the invoice note must be imported exactly once (not duplicated)",
        )

    def test_malformed_ubl_fails_cleanly_without_orphan_bill(self):
        """A decode error fails the import, keeps the JSON and leaves no orphan bill."""
        extracted = self._extracted()
        rec = self._new_import()
        before = self.env["account.move"].search_count([])
        with self._patch_extract(self._ai_result(extracted, b"<not-ubl")):
            result = rec._process_import()

        self.assertTrue(result.startswith("FAILED"), result)
        self.assertEqual(rec.state, "failed")
        self.assertFalse(rec.move_id, "no bill must be linked on failure")
        self.assertTrue(rec.extraction_data, "extraction JSON preserved for re-import")
        self.assertEqual(
            self.env["account.move"].search_count([]),
            before,
            "the discarded fresh move must be unlinked (no orphan bill)",
        )

    def test_manual_upload_routes_through_queue(self):
        """A manual PDF upload enqueues an ai.invoice.import instead of decoding inline."""
        att = self.env["ir.attachment"].create(
            {"name": "up.pdf", "raw": b"%PDF up", "mimetype": "application/pdf"}
        )
        journal = self.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", self.company.id)],
            limit=1,
        )
        with patch.object(
            type(self.env["ai.invoice.import"]),
            "with_delay",
            return_value=MagicMock(),
        ) as delay:
            action = journal.with_context(
                default_move_type="in_invoice"
            ).create_document_from_attachment(att.ids)

        delay.assert_called_once()
        self.assertEqual(
            delay.call_args.kwargs.get("channel"), "root.ai_import",
            "AI extraction jobs must run on the capped root.ai_import channel",
        )
        imp = self.env["ai.invoice.import"].search(
            [("pdf_attachment_id", "=", att.id)]
        )
        self.assertEqual(len(imp), 1, "one import record created for the upload")
        self.assertEqual(imp.company_id, self.company)
        self.assertEqual(
            imp.journal_id, journal, "the targeted upload journal must be preserved"
        )
        self.assertEqual(imp.upload_move_type, "in_invoice")
        self.assertEqual(action.get("res_model"), "ai.invoice.import")

    def test_sale_journal_upload_stays_native(self):
        """A customer-invoice upload must never reach the AI queue.

        Decoded as an `out_invoice`, a supplier PDF would put the supplier's IBAN
        on a document where the company is the payee.
        """
        att = self.env["ir.attachment"].create(
            {"name": "customer.pdf", "raw": b"%PDF s", "mimetype": "application/pdf"}
        )
        sale_journal = self.env["account.journal"].search(
            [("type", "=", "sale"), ("company_id", "=", self.company.id)], limit=1
        )
        self.assertTrue(sale_journal, "this database has no sale journal to test with")

        Journal = type(self.env["account.journal"])
        with patch.object(
            Journal,
            "_create_document_from_attachment",
            return_value=self.env["account.move"],
        ) as native:
            sale_journal.with_context(
                default_move_type="out_invoice"
            ).create_document_from_attachment(att.ids)

        native.assert_called_once()
        self.assertFalse(
            self.env["ai.invoice.import"].search([("pdf_attachment_id", "=", att.id)]),
            "a sale-journal upload must not create an AI import record",
        )

    def test_manual_upload_without_api_key_uses_native(self):
        """Without an API key the upload falls back to the native import."""
        self.company.claude_api_key = False
        self.addCleanup(
            lambda: self.company.write({"claude_api_key": "test-key"})
        )
        att = self.env["ir.attachment"].create(
            {"name": "native.pdf", "raw": b"%PDF n", "mimetype": "application/pdf"}
        )
        journal = self.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", self.company.id)],
            limit=1,
        )
        Journal = type(self.env["account.journal"])
        with patch.object(
            Journal,
            "_create_document_from_attachment",
            return_value=self.env["account.move"],
        ) as native:
            journal.with_context(
                default_move_type="in_invoice"
            ).create_document_from_attachment(att.ids)

        native.assert_called_once()
        self.assertFalse(
            self.env["ai.invoice.import"].search([("pdf_attachment_id", "=", att.id)]),
            "no AI import record when the company has no API key",
        )

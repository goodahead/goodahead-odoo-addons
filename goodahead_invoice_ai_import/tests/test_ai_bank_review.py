# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""A bank account printed on a PDF the AI read is never added to the vendor.

Invoice fraud is a changed bank account: a real vendor's name and VAT number
with somebody else's IBAN. The AI reader therefore imports under a policy
whose bank accounts are only LOOKED UP on the vendor: an account the vendor
already holds is used, a new one waits for a person as a sensitive vendor
data suggestion. Invented data throughout (``ZZ-…`` identifiers).
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_ai_import.models.ai_invoice_import import AI_READER

KNOWN_IBAN = "FI2912345600004521"
NEW_IBAN = "FI2112345600000785"


@tagged("post_install", "-at_install")
class TestAiBankReview(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.company.claude_api_key = "test-key"
        cls.Bank = cls.env["res.partner.bank"]
        cls.vendor = cls.env["res.partner"].create(
            {
                "name": "ZZ Review Vendor Oy",
                "is_company": True,
                "company_registry": "ZZ-REVIEW-0001",
                "country_id": cls.env.ref("base.fi").id,
            }
        )

    def _extracted(self, iban):
        return {
            "type": "Invoice",
            "invoice_number": "ZZ-RV-1",
            "issue_date": "2026-03-02",
            "due_date": "2026-03-30",
            "currency": "EUR",
            "seller": {
                "name": "ZZ Review Vendor Oy",
                "business_id": "ZZ-REVIEW-0001",
                "country_code": "FI",
                "iban": iban,
                "bic": "DABAFIHH",
            },
            "buyer": {"name": self.company.name, "country_code": "FI"},
            "lines": [
                {
                    "name": "ZZ consulting",
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
                {"tax_percent": 25.5, "taxable_amount": 100.0, "tax_amount": 25.5,
                 "tax_category": "S"}
            ],
            "totals": {"line_extension": 100.0, "tax_exclusive": 100.0,
                       "tax_inclusive": 125.5, "payable": 125.5},
        }

    def _import(self, extracted):
        attachment = self.env["ir.attachment"].create(
            {"name": "bill.pdf", "raw": b"%PDF-1.4 fake", "mimetype": "application/pdf"}
        )
        record = self.env["goodahead.invoice.import"].create(
            {
                "name": "bill.pdf",
                "company_id": self.company.id,
                "pdf_attachment_id": attachment.id,
                "reader": AI_READER,
            }
        )
        ai_result = {
            "extracted": extracted,
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "model_name": "test-model",
            "xml_bytes": self.env["account.move"]._ai_generate_ubl_from_extracted(
                extracted, self.company
            ),
        }
        with patch.object(
            type(self.env["account.move"]), "_ai_extract_and_generate_ubl",
            return_value=ai_result,
        ):
            result = record._process_import()
        self.assertTrue(result.startswith("OK"), result)
        return record.move_id

    def _suggestions(self, move):
        return self.env["goodahead.vendor.data.suggestion"].search(
            [("move_id", "=", move.id), ("field_name", "=", "bank_account"),
             ("state", "=", "pending")]
        )

    def test_a_new_iban_is_not_added_to_the_vendor_but_suggested(self):
        move = self._import(self._extracted(NEW_IBAN))
        self.assertEqual(move.partner_id, self.vendor)
        self.assertFalse(
            self.Bank.with_context(active_test=False).search(
                [("sanitized_acc_number", "=", NEW_IBAN)]
            ),
            "no bank account may be created from a PDF",
        )
        self.assertFalse(move.partner_bank_id)
        suggestion = self._suggestions(move)
        self.assertEqual(len(suggestion), 1)
        self.assertTrue(suggestion.is_sensitive)
        self.assertEqual(suggestion.partner_id, self.vendor)
        self.assertEqual(suggestion.invoice_value, NEW_IBAN)

    def test_an_iban_the_vendor_holds_is_used(self):
        known = self.Bank.create({"acc_number": KNOWN_IBAN, "partner_id": self.vendor.id})
        move = self._import(self._extracted(KNOWN_IBAN))
        self.assertEqual(move.partner_bank_id, known)
        self.assertFalse(self._suggestions(move))
        self.assertEqual(self.vendor.bank_ids, known)

    def test_the_ai_reader_looks_bank_accounts_up_only(self):
        reader = self.env["goodahead.invoice.import"]._import_readers()[AI_READER]
        self.assertTrue(reader.policy.bank_account_lookup_only)
        self.assertTrue(reader.policy.is_legacy, "vendor matching itself is unchanged")

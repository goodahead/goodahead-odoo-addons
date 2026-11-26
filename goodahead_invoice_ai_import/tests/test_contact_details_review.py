# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Contact details read off a document anybody can send wait for a person.

A forged PDF in a real vendor's name used to fill that vendor's empty email,
phone and website, and add the document's phone and web domain to the
vendor's identifiers — so purchase orders went to the sender and the next
forged bill matched the vendor by the sender's domain alone. For an AI
reading (and anything that arrived by email) those three now become pending
suggestions, and the identifiers are learned only once a person confirms the
vendor (picks it, creates it) or posts the bill. Invented data throughout.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_ai_import.models.ai_invoice_import import AI_READER


@tagged("post_install", "-at_install")
class TestContactDetailsReview(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.company.claude_api_key = "test-key"
        cls.env.ref("base.EUR").active = True
        cls.Identifier = cls.env["goodahead.partner.identifier"]
        cls.vendor = cls.env["res.partner"].create(
            {
                "name": "ZZ Contact Vendor Oy",
                "is_company": True,
                "company_registry": "ZZ-CONTACT-0001",
                "country_id": cls.env.ref("base.fi").id,
            }
        )

    def _extracted(self):
        return {
            "type": "Invoice",
            "invoice_number": "ZZ-CD-1",
            "issue_date": "2026-03-02",
            "due_date": "2026-03-30",
            "currency": "EUR",
            "seller": {
                "name": "ZZ Contact Vendor Oy",
                "business_id": "ZZ-CONTACT-0001",
                "country_code": "FI",
                "street": "Esimerkkikatu 1",
                "city": "Esimerkki",
                "phone": "+358 40 555 0142",
                "email": "billing@zz-forged.example",
                "website": "https://www.zz-forged.example",
            },
            "buyer": {"name": self.company.name, "country_code": "FI"},
            "lines": [
                {"name": "ZZ service", "quantity": 1.0, "unit_code": "C62",
                 "price_unit": 100.0, "line_total": 100.0, "tax_percent": 0.0,
                 "tax_amount": 0.0, "tax_category": "Z"}
            ],
            "tax_subtotals": [
                {"tax_percent": 0.0, "taxable_amount": 100.0, "tax_amount": 0.0,
                 "tax_category": "Z"}
            ],
            "totals": {"line_extension": 100.0, "tax_exclusive": 100.0,
                       "tax_inclusive": 100.0, "payable": 100.0},
        }

    def _import(self, source="upload"):
        extracted = self._extracted()
        attachment = self.env["ir.attachment"].create(
            {"name": "bill.pdf", "raw": b"%PDF-1.4 fake", "mimetype": "application/pdf"}
        )
        record = self.env["goodahead.invoice.import"].create(
            {
                "name": "bill.pdf",
                "company_id": self.company.id,
                "pdf_attachment_id": attachment.id,
                "reader": AI_READER,
                "source": source,
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

    def _identifiers(self):
        return self.Identifier.search([("partner_id", "=", self.vendor.id)])

    def _pending(self, move, fields=("phone", "email", "website")):
        return self.env["goodahead.vendor.data.suggestion"].search(
            [("move_id", "=", move.id), ("field_name", "in", list(fields)),
             ("state", "=", "pending")]
        )

    def test_contact_details_wait_for_a_person_and_nothing_is_learned(self):
        before = self._identifiers()
        move = self._import()
        self.assertEqual(move.partner_id, self.vendor)
        self.assertFalse(self.vendor.phone)
        self.assertFalse(self.vendor.email)
        self.assertFalse(self.vendor.website)
        self.assertEqual(
            sorted(self._pending(move).mapped("field_name")), ["email", "phone", "website"]
        )
        self.assertEqual(self._identifiers(), before, "no identifier learned from the PDF")
        # The control: an empty address field is still filled.
        self.assertEqual(self.vendor.street, "Esimerkkikatu 1")

    def test_an_emailed_document_is_treated_the_same(self):
        move = self._import(source="email")
        self.assertFalse(self.vendor.email)
        self.assertEqual(len(self._pending(move)), 3)

    def test_posting_the_bill_teaches_the_identifiers(self):
        move = self._import()
        self.assertFalse(self._identifiers())
        move.invoice_date = move.invoice_date or "2026-03-02"
        move.action_post()
        kinds = sorted(self._identifiers().mapped("kind"))
        self.assertIn("phone", kinds)
        self.assertIn("domain", kinds)

    def test_picking_the_vendor_teaches_the_identifiers(self):
        move = self._import()
        self.env["goodahead.vendor.candidate"].create(
            {"move_id": move.id, "partner_id": self.vendor.id, "reason": "picked"}
        ).action_use_this()
        self.assertTrue(self._identifiers())

    def test_applying_a_suggestion_fills_the_empty_field(self):
        move = self._import()
        phone = self._pending(move, ("phone",))
        phone.action_apply()
        self.assertEqual(self.vendor.phone, "+358 40 555 0142")

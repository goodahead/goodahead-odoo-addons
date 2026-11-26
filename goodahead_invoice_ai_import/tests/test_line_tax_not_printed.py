# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The bill carries the PDF's lines only; a difference is a red banner.

The receipt below prints one tax amount for the whole order and none per line.
It must import with exactly its own lines, no added tax line, and a red banner
when the bill's totals differ from the document's. Driven through the re-import button.
"""

import copy
import json
from unittest.mock import patch

from odoo import Command
from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_import.adapters.ubl_generator import (
    generate_peppol_invoice_xml,
)


def _line(name, qty, price, total, description=None, category="goods"):
    return {
        "product_code": None,
        "name": name,
        "description": description,
        "quantity": qty,
        "unit_code": "C62",
        "price_unit": price,
        "line_total": total,
        "tax_percent": 8.89,
        "tax_amount": None,
        "tax_category": "S",
        "expense_category": category,
    }


RESTAURANT_RECEIPT = {
    "type": "Invoice",
    "invoice_number": "ZZ-000417",
    "issue_date": "2026-01-15",
    "due_date": "2026-01-15",
    "currency": "USD",
    "currency_symbol": "$",
    "seller": {
        "name": "ZZ Sample Grill",
        "vat": None,
        "business_id": None,
        "street": "100 Example Avenue",
        "city": "New York",
        "state_code": "NY",
        "postal_code": "10001",
        "country_code": "US",
        "phone": None,
        "email": None,
        "website": None,
        "routing_number": None,
        "account_number": None,
        "iban": None,
        "bic": None,
        "remit_to_address": None,
    },
    "buyer": {
        "name": "Example Holdings LLC",
        "vat": None,
        "street": "1 Example Plaza",
        "city": "New York",
        "state_code": "NY",
        "postal_code": "10002",
        "country_code": "US",
    },
    "payment_reference": None,
    "payment_means_code": None,
    "note": "The invoice prints one tax amount but no per-line tax.",
    "lines": [
        _line("ZZ Club Sandwich", 3, 18.45, 55.35, "Bread: Sourdough"),
        _line("ZZ Soup of the Day", 4, 9.8, 39.2),
        _line("ZZ French Fries", 2, 8.65, 17.3, "Size: Large"),
        _line("ZZ Side Salad", 2, 6.95, 13.9, "Size: Regular"),
        _line("ZZ Onion Rings", 3, 6.4, 19.2),
        _line("ZZ Iced Tea", 4, 4.1, 16.4),
        _line("House Credit", 1, -10.0, -10.0, "Deduction applied before tax",
              category="discounts"),
    ],
    # Invented figures, chosen so that the printed tax (13.46 on 151.35) gives
    # 8.89%, taxing every line at 8.89% reproduces it to the cent, and taxing
    # at New York's 8.875% gives 13.43 both per line and on the total: 3 cents
    # short, just past the 0.02 tolerance.
    "tax_subtotals": [
        {
            "tax_percent": 8.89,
            "taxable_amount": 151.35,
            "tax_amount": 13.46,
            "tax_category": "S",
        }
    ],
    "totals": {
        "line_extension": 151.35,
        "tax_exclusive": 151.35,
        "tax_inclusive": 164.81,
        "payable": 164.81,
    },
}


def _negated(extracted):
    """The same document as a credit note printed with negative amounts."""
    data = copy.deepcopy(extracted)
    data["type"] = "CreditNote"
    for line in data["lines"]:
        for key in ("quantity", "line_total"):
            line[key] = -line[key]
    for sub in data["tax_subtotals"]:
        for key in ("taxable_amount", "tax_amount"):
            sub[key] = -sub[key]
    for key in data["totals"]:
        data["totals"][key] = -data["totals"][key]
    return data


@tagged("post_install", "-at_install")
class TestTheBillCarriesThePdfLinesOnly(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.company.claude_api_key = "test-key"
        cls.env.ref("base.USD").active = True
        # Precondition: no purchase tax exists at the printed rate, or the
        # banner tests would see a matching bill.
        cls.env["account.tax"].search(
            [
                ("type_tax_use", "=", "purchase"),
                ("amount", "=", 8.89),
                ("company_id", "=", cls.company.id),
            ]
        ).active = False

    def setUp(self):
        super().setUp()
        # 18.0 builds from April 2026 on cache tax lookups on the cursor
        # (`cr.cache['retrieved_tax_map']`), which every test of this class
        # shares: a "no 8.89% tax" answer from one test would hide the tax a
        # later test creates. Not present on other builds.
        cache = getattr(self.env.cr, "cache", None)
        if isinstance(cache, dict):
            cache.pop("retrieved_tax_map", None)

    def _reimport(self, extracted, move_type="in_invoice"):
        """Run the re-import button on a failed import with the JSON stored."""
        att = self.env["ir.attachment"].create(
            {"name": "receipt.pdf", "raw": b"%PDF-1.4 fake",
             "mimetype": "application/pdf"}
        )
        rec = self.env["goodahead.invoice.import"].create(
            {
                "name": "receipt.pdf",
                "company_id": self.company.id,
                "pdf_attachment_id": att.id,
                "state": "failed",
                "upload_move_type": move_type,
                "extraction_data": json.dumps(extracted),
            }
        )
        with patch.object(
            type(self.env["account.move"]),
            "_ai_extract_and_generate_ubl",
            side_effect=AssertionError("the re-import must not call Claude"),
        ):
            rec.action_reimport_from_json()
        self.assertEqual(rec.state, "imported", rec.error_message or "")
        self.assertTrue(rec.move_id, "the re-import produced no bill")
        return rec.move_id

    def _product_lines(self, move):
        return move.invoice_line_ids.filtered(
            lambda line: line.display_type == "product"
        )

    def _purchase_tax(self, amount):
        return self.env["account.tax"].create(
            {
                "name": "ZZ Test %s%% Purchase" % amount,
                "type_tax_use": "purchase",
                "amount_type": "percent",
                "amount": amount,
                "company_id": self.company.id,
            }
        )

    # ------------------------------------------------------------------
    # The receipt with no per-line tax
    # ------------------------------------------------------------------

    def test_the_receipt_imports_with_its_seven_lines_and_a_red_banner(self):
        """No tax at 8.89%: the bill is exactly the receipt's seven lines,
        $151.35, and the banner states both totals and both taxes."""
        move = self._reimport(copy.deepcopy(RESTAURANT_RECEIPT))

        lines = self._product_lines(move)
        self.assertEqual(len(lines), 7, lines.mapped("name"))
        self.assertFalse(
            any((line.name or "").startswith("Sales tax") for line in lines),
            "no line may be added for the tax",
        )
        self.assertAlmostEqual(move.amount_total, 151.35, places=2)
        self.assertAlmostEqual(move.ai_document_total, 164.81, places=2)
        self.assertAlmostEqual(move.ai_document_tax, 13.46, places=2)
        self.assertTrue(move.ai_total_mismatch)
        self.assertTrue(move.ai_tax_mismatch)

    def test_fixing_the_bill_clears_the_banner(self):
        """The banner reads the live figures: a person who sets the right tax
        makes it go away."""
        move = self._reimport(copy.deepcopy(RESTAURANT_RECEIPT))
        tax = self._purchase_tax(8.89)

        self._product_lines(move).write({"tax_ids": [Command.set(tax.ids)]})

        self.assertAlmostEqual(move.amount_total, 164.81, delta=0.02)
        self.assertFalse(move.ai_total_mismatch)
        self.assertFalse(move.ai_tax_mismatch)

    def test_taxed_at_the_ny_rate_the_banner_shows_the_three_cents(self):
        """The receipt's own rate is 8.89% (13.46 / 151.35); New York's is
        8.875%. Taxed at New York's rate the bill is 164.78 against 164.81, and
        that 3-cent difference is above the 0.02 tolerance: it must show."""
        move = self._reimport(copy.deepcopy(RESTAURANT_RECEIPT))
        ny = self._purchase_tax(8.875)

        self._product_lines(move).write({"tax_ids": [Command.set(ny.ids)]})

        self.assertAlmostEqual(move.amount_tax, 13.43, places=2)
        self.assertAlmostEqual(move.amount_total, 164.78, places=2)
        self.assertTrue(move.ai_total_mismatch)
        self.assertTrue(move.ai_tax_mismatch)

    def test_a_right_total_with_the_wrong_tax_still_shows_the_banner(self):
        """Tax typed as an expense line by hand: the total agrees, the tax does
        not, and that is still worth a look."""
        move = self._reimport(copy.deepcopy(RESTAURANT_RECEIPT))

        move.write(
            {
                "invoice_line_ids": [
                    Command.create(
                        {"name": "Sales tax", "quantity": 1,
                         "price_unit": 13.46, "tax_ids": [Command.clear()]}
                    )
                ]
            }
        )

        self.assertAlmostEqual(move.amount_total, 164.81, places=2)
        self.assertTrue(move.ai_total_mismatch)
        self.assertTrue(move.ai_tax_mismatch)

    # ------------------------------------------------------------------
    # The control: a bill that matches shows nothing
    # ------------------------------------------------------------------

    def test_a_document_odoo_can_tax_shows_no_banner(self):
        """Control: a matching bill shows no banner."""
        self._purchase_tax(8.89)

        move = self._reimport(copy.deepcopy(RESTAURANT_RECEIPT))

        self.assertEqual(len(self._product_lines(move)), 7)
        self.assertAlmostEqual(move.amount_total, 164.81, delta=0.02)
        self.assertFalse(move.ai_total_mismatch)

    def test_a_difference_of_exactly_the_tolerance_shows_no_banner(self):
        """0.02 is inside; 0.03 is not. In floating point 164.81 - 164.79 is a
        hair over 0.02, which a bare `>` would flag."""
        self._purchase_tax(8.89)
        move = self._reimport(copy.deepcopy(RESTAURANT_RECEIPT))
        total = move.amount_total
        tax = move.amount_tax

        move.write({"ai_document_total": total + 0.02, "ai_document_tax": tax + 0.02})
        self.assertFalse(move.ai_total_mismatch)

        move.write({"ai_document_total": total + 0.03, "ai_document_tax": tax})
        self.assertTrue(move.ai_total_mismatch)
        self.assertFalse(move.ai_tax_mismatch)

    def test_a_hand_edited_string_amount_is_read_like_the_bill_reads_it(self):
        """A re-import is not re-validated, so "13.46" typed as text reaches
        the bill as 13.46. The stored figures must read it the same way, or a
        matching bill shows a false red banner."""
        self._purchase_tax(8.89)
        extracted = copy.deepcopy(RESTAURANT_RECEIPT)
        extracted["tax_subtotals"][0]["tax_amount"] = "13.46"
        extracted["totals"]["payable"] = "164.81"

        move = self._reimport(extracted)

        self.assertAlmostEqual(move.ai_document_tax, 13.46, places=2)
        self.assertAlmostEqual(move.ai_document_total, 164.81, places=2)
        self.assertFalse(move.ai_total_mismatch)

    def test_a_document_with_no_tax_shows_no_banner(self):
        extracted = copy.deepcopy(RESTAURANT_RECEIPT)
        extracted["tax_subtotals"] = []
        for line in extracted["lines"]:
            line["tax_amount"] = 0.0
            line["tax_percent"] = 0.0
            line["tax_category"] = "Z"
        extracted["totals"]["tax_inclusive"] = 151.35
        extracted["totals"]["payable"] = 151.35

        move = self._reimport(extracted)

        self.assertAlmostEqual(move.amount_total, 151.35, places=2)
        self.assertFalse(move.ai_total_mismatch)

    def test_a_bill_from_before_the_check_never_shows_the_banner(self):
        """Nothing was stored on older bills; a zero must not read as "the
        invoice total is $0.00"."""
        move = self._reimport(copy.deepcopy(RESTAURANT_RECEIPT))
        move.write({"ai_check_total": False, "ai_document_total": 0.0,
                    "ai_document_tax": 0.0})

        self.assertFalse(move.ai_total_mismatch)

    def test_the_banner_is_red_and_on_the_form(self):
        arch = self.env["account.move"].get_view(
            self.env.ref("account.view_move_form").id
        )["arch"]

        self.assertIn("o_ai_total_mismatch alert alert-danger", arch)
        self.assertIn("Review before posting", arch)

    # ------------------------------------------------------------------
    # Credit notes
    # ------------------------------------------------------------------

    def test_a_credit_note_printed_negative_compares_magnitudes(self):
        """`in_refund` holds magnitudes. A credit note printed with negative
        amounts must store +164.81 and +13.46, not figures that can never
        match."""
        move = self._reimport(_negated(RESTAURANT_RECEIPT), move_type="in_refund")

        self.assertEqual(move.move_type, "in_refund")
        self.assertEqual(len(self._product_lines(move)), 7)
        self.assertAlmostEqual(move.amount_total, 151.35, places=2)
        self.assertAlmostEqual(move.ai_document_total, 164.81, places=2)
        self.assertAlmostEqual(move.ai_document_tax, 13.46, places=2)
        self.assertTrue(move.ai_total_mismatch)

    def test_a_matching_credit_note_printed_negative_shows_no_banner(self):
        self._purchase_tax(8.89)

        move = self._reimport(_negated(RESTAURANT_RECEIPT), move_type="in_refund")

        self.assertEqual(move.move_type, "in_refund")
        self.assertFalse(move.ai_total_mismatch)

    def test_a_credit_note_printed_positive_compares_the_same_way(self):
        extracted = copy.deepcopy(RESTAURANT_RECEIPT)
        extracted["type"] = "CreditNote"

        move = self._reimport(extracted, move_type="in_refund")

        self.assertEqual(move.move_type, "in_refund")
        self.assertAlmostEqual(move.ai_document_total, 164.81, places=2)
        self.assertTrue(move.ai_total_mismatch)

    # ------------------------------------------------------------------
    # Only the line tax may be null
    # ------------------------------------------------------------------

    def test_the_forward_path_imports_too(self):
        """The import that calls Claude, with the call patched."""
        extracted = copy.deepcopy(RESTAURANT_RECEIPT)
        att = self.env["ir.attachment"].create(
            {"name": "receipt.pdf", "raw": b"%PDF-1.4 fake",
             "mimetype": "application/pdf"}
        )
        rec = self.env["goodahead.invoice.import"].create(
            {"name": "receipt.pdf", "company_id": self.company.id,
             "pdf_attachment_id": att.id}
        )
        ai_result = {
            "extracted": extracted,
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "model_name": "test-model",
            "xml_bytes": generate_peppol_invoice_xml(extracted),
        }
        with patch.object(
            type(self.env["account.move"]),
            "_ai_extract_and_generate_ubl",
            return_value=ai_result,
        ):
            result = rec._process_import()

        self.assertTrue(result.startswith("OK"), result)
        self.assertEqual(len(self._product_lines(rec.move_id)), 7)
        self.assertTrue(rec.move_id.ai_total_mismatch)

    def test_a_null_line_total_still_fails_the_import(self):
        """Control: only the line tax may be null, not the line total."""
        extracted = copy.deepcopy(RESTAURANT_RECEIPT)
        extracted["lines"][0]["line_total"] = None
        att = self.env["ir.attachment"].create(
            {"name": "receipt.pdf", "raw": b"%PDF-1.4 fake",
             "mimetype": "application/pdf"}
        )
        rec = self.env["goodahead.invoice.import"].create(
            {"name": "receipt.pdf", "company_id": self.company.id,
             "pdf_attachment_id": att.id, "state": "failed",
             "extraction_data": json.dumps(extracted)}
        )

        rec.action_reimport_from_json()

        self.assertEqual(rec.state, "failed")
        self.assertFalse(rec.move_id)
        self.assertIn("line_total", rec.error_message)

# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The Finnish invoice PDF and its bank bar code.

A payable invoice carries the bar code (version 4 or 5 by its reference); a
credit note and an invoice with nothing to pay carry none.
"""

import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ERP.
    raise unittest.SkipTest("needs the ERP")

from odoo.tests import tagged  # noqa: E402

from odoo.addons.account.tests.common import AccountTestInvoicingCommon  # noqa: E402

from ..lib.fi_bank_barcode import BARCODE_LENGTH  # noqa: E402
from ..models.account_move import NOTHING_TO_PAY  # noqa: E402

INVOICE_REPORT = "account.account_invoices"
BARCODE_IMG = "/report/barcode?barcode_type=Code128"


@tagged("post_install", "-at_install")
class TestFinnishInvoiceReport(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.company_data["company"]
        finland = cls.env.ref("base.fi")
        cls.company.write({"country_id": finland.id, "account_fiscal_country_id": finland.id})
        cls.eur = cls.env.ref("base.EUR")
        if cls.company.currency_id != cls.eur:
            cls.eur = cls.setup_other_currency("EUR", rates=[("2000-01-01", 1.0)])
        cls.env["res.partner.bank"].create({
            "acc_number": "FI21 1234 5600 0007 85",
            "partner_id": cls.company.partner_id.id,
            # An untrusted company account blocks posting.
            "allow_out_payment": True,
        })
        cls.customer = cls.env["res.partner"].create({"name": "Example Buyer Oy"})

    def _invoice(self, move_type="out_invoice", amount=100.0, reference="10001235"):
        move = self.init_invoice(
            move_type, partner=self.customer, amounts=[amount], taxes=[], currency=self.eur,
        )
        move.payment_reference = reference
        move.action_post()
        return move

    def _render(self, move):
        return self.env["ir.actions.report"]._render_qweb_html(INVOICE_REPORT, move.ids)[0].decode()

    def test_payable_invoice_with_a_finnish_reference_has_version_4(self):
        move = self._invoice(reference="10001235")
        barcode = move.fi_bank_barcode
        self.assertTrue(barcode, move._fi_bank_barcode_error())
        self.assertEqual(len(barcode), BARCODE_LENGTH)
        self.assertEqual(barcode[0], "4")
        self.assertEqual(barcode[1:17], "2112345600000785")
        self.assertEqual(barcode[17:25], "00010000")
        self.assertTrue(barcode[28:48].endswith("10001235"))
        self.assertEqual(barcode[48:], move.invoice_date_due.strftime("%y%m%d"))
        html = self._render(move)
        self.assertIn(BARCODE_IMG, html)
        self.assertIn(barcode, html)

    def test_payable_invoice_with_a_creditor_reference_has_version_5(self):
        """A reference written with spaces and a lower-case rf is normalised."""
        move = self._invoice(reference="rf18 5390 0754 7034")
        barcode = move.fi_bank_barcode
        self.assertTrue(barcode, move._fi_bank_barcode_error())
        self.assertEqual(barcode[0], "5")
        self.assertEqual(barcode[25:27], "18")
        self.assertTrue(barcode[27:48].endswith("539007547034"))
        self.assertIn(barcode, self._render(move))

    def test_credit_note_has_no_barcode(self):
        move = self._invoice(move_type="out_refund")
        self.assertTrue(move._fi_uses_finnish_invoice_report())
        self.assertFalse(move.fi_bank_barcode)
        self.assertEqual(move._fi_bank_barcode_error(), "only an invoice asks the customer to pay")
        self.assertNotIn(BARCODE_IMG, self._render(move))

    def test_paid_invoice_has_no_barcode(self):
        move = self._invoice()
        self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=move.ids
        ).create({})._create_payments()
        self.assertEqual(move.payment_state in ("paid", "in_payment"), True)
        move.invalidate_recordset(["fi_bank_barcode"])
        self.assertFalse(move.fi_bank_barcode)
        self.assertEqual(move._fi_bank_barcode_error(), NOTHING_TO_PAY)
        self.assertNotIn(BARCODE_IMG, self._render(move))

    def test_zero_invoice_has_no_barcode(self):
        move = self._invoice(amount=0.0)
        self.assertFalse(move.fi_bank_barcode)
        self.assertEqual(move._fi_bank_barcode_error(), NOTHING_TO_PAY)
        self.assertNotIn(BARCODE_IMG, self._render(move))

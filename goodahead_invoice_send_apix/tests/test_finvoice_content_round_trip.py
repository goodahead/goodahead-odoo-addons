# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The round trip (finvoice content T16; AC-23): an invoice with every new
content is sent (recording client, no network), its message read back by the
base's Finvoice reader and - where the import core is installed - imported
as a bill, which carries the same lines, quantities, prices, discounts, VAT
rates, categories as the import maps them, references and totals; the text
rows land where the import places them today. Every value is invented."""

import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.tests import tagged

from odoo.addons.goodahead_einvoice_base.lib import finvoice

from .common import FakeApixSendClient, SendApixCommon


@tagged("post_install", "-at_install")
class TestFinvoiceContentRoundTrip(SendApixCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.s24 = cls._make_tax(cls.company, 24.0, "S")
        cls.ae0 = cls._make_tax(cls.company, 0.0, "AE")
        cls.product = cls.env["product.product"].create(
            {"name": "ZZ Barcoded", "barcode": "4006381333931", "default_code": "ZZ-B"})
        cls.site = cls.env["res.partner"].create({
            "name": "ZZ Site B", "type": "delivery", "parent_id": cls.customer.id,
            "street": "Toimituskatu 5", "zip": "20100", "city": "Turku",
            "country_id": cls.finland.id,
        })

    def _sent_message(self):
        fake = FakeApixSendClient().patch(self)
        move = self._make_invoice(
            lines=[
                {"display_type": "line_section", "name": "Section A"},
                {"name": "ZZ discounted", "quantity": 10, "price_unit": 100.0,
                 "discount": 15.0, "tax_ids": [(6, 0, self.s24.ids)]},
                {"display_type": "line_note", "name": "Delivered in two parts"},
                {"name": "ZZ Barcoded", "product_id": self.product.id, "quantity": 2,
                 "price_unit": 50.0, "tax_ids": [(6, 0, self.s24.ids)]},
                {"name": "ZZ reverse charge", "quantity": 1, "price_unit": 200.0,
                 "tax_ids": [(6, 0, self.ae0.ids)]},
            ],
            ref="PO-4711, S00012", invoice_origin="S00012",
            einvoice_buyer_reference="Test User", payment_reference="RF471234567890",
            partner_shipping_id=self.site.id,
        )
        result = move._apix_send_now("button")
        self.assertEqual(result.outcome, "accepted")
        self.assertEqual(len(fake.calls), 1)
        message = self._row(result).sudo().message_attachment_id.raw
        self.assertEqual(finvoice.validate_schema(message), [])
        return move, message

    def test_message_reads_back_with_every_content(self):
        move, message = self._sent_message()
        data = finvoice.to_invoice_data(finvoice.parse(message)[0])
        self.assertEqual(data["order_reference"], "PO-4711")
        self.assertEqual(data["buyer_reference"], "Test User")
        self.assertEqual(data["payment_reference"], "RF471234567890")
        self.assertTrue(data["note"].startswith("Section A"))
        lines = data["lines"]
        self.assertEqual([line["name"] for line in lines],
                         ["ZZ discounted", "ZZ Barcoded", "ZZ reverse charge"])
        discounted = lines[0]
        self.assertEqual((discounted["quantity"], discounted["gross_price"],
                          discounted["discount_percent"], discounted["discount_amount"],
                          discounted["line_total"]), (10.0, 100.0, 15.0, 150.0, 850.0))
        self.assertIn("Delivered in two parts", discounted["description"])
        self.assertEqual([line["tax_category"] for line in lines], ["S", "S", "AE"])
        self.assertEqual([line["tax_percent"] for line in lines], [24.0, 24.0, 0.0])
        self.assertAlmostEqual(data["totals"]["tax_exclusive"], move.amount_untaxed)
        self.assertAlmostEqual(data["totals"]["tax_inclusive"], move.amount_total)
        self.assertEqual(
            sorted((sub["tax_percent"], sub["tax_category"]) for sub in data["tax_subtotals"]),
            [(0.0, "AE"), (24.0, "S")])

    def test_message_imports_back_as_the_same_bill(self):
        if "goodahead.invoice.import" not in self.env:
            self.skipTest("the import core is not installed in this database")
        from odoo.addons.goodahead_invoice_import.lib.import_source import STRICT_POLICY
        from odoo.addons.goodahead_invoice_import.tests.common import ensure_purchase_journal

        for rate, category in ((24.0, False), (0.0, "AE")):
            self.env["account.tax"].create({
                "name": f"ZZ purchase {rate:g}", "amount": rate, "type_tax_use": "purchase",
                "company_id": self.company.id, "ubl_cii_tax_category_code": category})
        ensure_purchase_journal(self.env, self.company)
        move, message = self._sent_message()
        data = finvoice.to_invoice_data(finvoice.parse(message)[0])
        record = self.env["goodahead.invoice.import"]._create_for_source(
            "upload", self.company, [("zz-round-trip.xml", message, "application/xml")])
        result = record._import_from_invoice_data(data, STRICT_POLICY)
        self.assertEqual(result.outcome, "imported", record.error_message or "")
        bill = result.move
        self.assertEqual(bill.invoice_origin, "PO-4711")
        self.assertEqual(bill.einvoice_buyer_reference, "Test User")
        self.assertEqual(bill.ref, move.name)
        lines = bill.invoice_line_ids.filtered(lambda line: line.display_type == "product")
        self.assertEqual([(line.quantity, line.price_unit, line.discount) for line in lines],
                         [(10.0, 100.0, 15.0), (2.0, 50.0, 0.0), (1.0, 200.0, 0.0)])
        self.assertEqual(lines.mapped("price_subtotal"), [850.0, 100.0, 200.0])
        self.assertAlmostEqual(bill.amount_untaxed, move.amount_untaxed)
        self.assertAlmostEqual(bill.amount_total, move.amount_total)

    # -- the bill reproduces every stated row amount (review F1) -------------------

    def _setup_import(self):
        if "goodahead.invoice.import" not in self.env:
            self.skipTest("the import core is not installed in this database")
        from odoo.addons.goodahead_invoice_import.tests.common import ensure_purchase_journal

        for rate, category in ((24.0, False), (0.0, "AE"), (0.0, "Z"), (0.0, "E")):
            self.env["account.tax"].create({
                "name": f"ZZ purchase {rate:g} {category or ''}".strip(), "amount": rate,
                "type_tax_use": "purchase", "company_id": self.company.id,
                "ubl_cii_tax_category_code": category})
        ensure_purchase_journal(self.env, self.company)

    def _send_and_import(self, move):
        """Send ``move`` (recording client), read its message back and import
        it as a bill; returns ``(stated rows, bill)``."""
        from odoo.addons.goodahead_invoice_import.lib.import_source import STRICT_POLICY

        result = move._apix_send_now("button")
        self.assertEqual(result.outcome, "accepted")
        message = self._row(result).sudo().message_attachment_id.raw
        self.assertEqual(finvoice.validate_schema(message), [])
        data = finvoice.to_invoice_data(finvoice.parse(message)[0])
        record = self.env["goodahead.invoice.import"]._create_for_source(
            "upload", self.company, [(f"zz-{move.id}.xml", message, "application/xml")])
        imported = record._import_from_invoice_data(data, STRICT_POLICY)
        self.assertEqual(imported.outcome, "imported", record.error_message or "")
        rows = [row for row in data["lines"] if row.get("line_total") is not None]
        return rows, imported.move

    def _assert_bill_reproduces(self, move, rows, bill):
        lines = bill.invoice_line_ids.filtered(lambda line: line.display_type == "product")
        self.assertEqual(len(lines), len(rows))
        for line, row in zip(lines, rows):
            self.assertAlmostEqual(line.price_subtotal, row["line_total"], places=2,
                                   msg=f"{line.name}: {line.quantity} x {line.price_unit} "
                                       f"- {line.discount} %")
        self.assertAlmostEqual(bill.amount_untaxed, move.amount_untaxed, places=2)
        self.assertAlmostEqual(bill.amount_total, move.amount_total, places=2)

    def test_price_included_discount_on_a_large_quantity(self):
        """1000 x 0.99 with 24 % included and 5 % off goes out at the gross
        price 0.79839, discount 39.92 and amount 758.47; the ERP stores a
        price in 2 decimals (0.80 x 1000 less 5 % is 760.00), so the bill
        line must not keep that price."""
        self._setup_import()
        FakeApixSendClient().patch(self)
        included = self._make_tax(self.company, 24.0, "S", name="ZZ 24 % incl.")
        included.price_include_override = "tax_included"
        move = self._make_invoice(lines=[{
            "name": "ZZ many small", "quantity": 1000, "price_unit": 0.99, "discount": 5.0,
            "tax_ids": [(6, 0, included.ids)]}])
        rows, bill = self._send_and_import(move)
        self.assertAlmostEqual(rows[0]["gross_price"], 0.79839, places=5)
        self.assertAlmostEqual(rows[0]["line_total"], 758.47, places=2)
        self._assert_bill_reproduces(move, rows, bill)

    def test_harder_rows_reproduce_the_stated_amounts(self):
        """review G1: send -> import as a bill over a credit note, a
        price-included discount, a 100 % discount, a price per several
        units and the intra-EU and export categories; every bill line makes
        its stated row amount to the cent and the totals are the invoice's."""
        self._setup_import()
        FakeApixSendClient().patch(self)
        included = self._make_tax(self.company, 24.0, "S", name="ZZ 24 % incl. G1")
        included.price_include_override = "tax_included"
        k0 = self._make_tax(self.company, 0.0, "K")
        g0 = self._make_tax(self.company, 0.0, "G")

        def line(name, quantity, price, discount=0.0, tax=None, **extra):
            return {"name": name, "quantity": quantity, "price_unit": price,
                    "discount": discount, "tax_ids": [(6, 0, (tax or self.s24).ids)], **extra}

        hundred = {"product_uom_id": self.unit_hundred.id}
        cases = {
            "credit note": {"move_type": "out_refund", "lines": [
                line("ZZ returned", 2, 50.0, 20.0), line("ZZ fee", 1, 12.5)]},
            "price-included discount": {"lines": [
                line("ZZ many small", 1000, 0.99, 5.0, included),
                line("ZZ chairs", 10, 100.0, 15.0, included)]},
            "100 % discount": {"lines": [
                line("ZZ free", 4, 12.5, 100.0), line("ZZ paid", 1, 80.0)]},
            "price per several units": {"lines": [
                line("ZZ per hundred", 2.5, 40.0, 10.0, **hundred),
                line("ZZ per hundred plain", 3, 12.0, **hundred)]},
            "intra-EU supply and export": {"lines": [
                line("ZZ EU goods", 2, 150.0, 0.0, k0), line("ZZ export", 5, 19.9, 10.0, g0)]},
        }
        for label, case in cases.items():
            with self.subTest(case=label):
                move_type = case.get("move_type", "out_invoice")
                move = self._make_invoice(lines=case["lines"], move_type=move_type)
                rows, bill = self._send_and_import(move)
                self.assertEqual(bill.move_type,
                                 "in_refund" if move_type == "out_refund" else "in_invoice")
                self._assert_bill_reproduces(move, rows, bill)

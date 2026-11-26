# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""References and discounted rows on imported bills (finvoice content T15;
AC-21, AC-22).

The core is source-agnostic: the invoice data the Finvoice reader of the
base gives is handed to ``_import_from_invoice_data`` as the Finvoice source
does (upload, mail and APIX all end there). Every fixture is invented.
"""

import copy
import glob
import json
import os

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_einvoice_base.lib import finvoice
from odoo.addons.goodahead_invoice_import.lib.import_source import STRICT_POLICY

from .common import ensure_purchase_journal

BASE_DATA = os.path.join(
    os.path.dirname(finvoice.__file__), os.pardir, os.pardir, "tests", "data")


def _invoice_data(name, index=0):
    with open(os.path.join(BASE_DATA, name), "rb") as handle:
        messages = finvoice.parse(handle.read())
    return finvoice.to_invoice_data(messages[index])


def _plain(value):
    """The reader snapshot's JSON form (dates and decimals as strings)."""
    return json.loads(json.dumps(value, sort_keys=True, default=str))


class _RollBack(Exception):
    """Leaves a savepoint without keeping what was imported in it."""


@tagged("post_install", "-at_install")
class TestFinvoiceContentImport(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.journal = ensure_purchase_journal(cls.env, cls.company)
        cls.env.ref("base.EUR").active = True
        Tax = cls.env["account.tax"]
        for rate in (25.5, 24.0, 14.0, 10.0, 0.0):
            if not Tax.search([("type_tax_use", "=", "purchase"), ("amount_type", "=", "percent"),
                               ("amount", "=", rate), ("company_id", "=", cls.company.id)],
                              limit=1):
                Tax.create({"name": f"ZZ {rate} % Purchase", "type_tax_use": "purchase",
                            "amount_type": "percent", "amount": rate,
                            "company_id": cls.company.id})
        cls.env["res.partner"].create({
            "name": "Example Supplier Oy", "is_company": True, "vat": "FI00000035",
            "company_registry": "0000003-5", "country_id": cls.env.ref("base.fi").id,
        })

    def _import(self, data):
        record = self.env["goodahead.invoice.import"]._create_for_source(
            "upload", self.company, [("zz-finvoice.xml", b"<zz/>", "application/xml")])
        result = record._import_from_invoice_data(data, STRICT_POLICY)
        self.assertEqual(result.outcome, "imported", record.error_message or "")
        return record, result.move

    @staticmethod
    def _product_lines(move):
        return move.invoice_line_ids.filtered(lambda line: line.display_type == "product")

    # -- AC-21 -------------------------------------------------------------------

    def test_references_on_the_record_and_the_bill(self):
        record, bill = self._import(_invoice_data("fi30_references_discounts.xml"))
        self.assertEqual(record.einvoice_order_reference, "PO-ZZ-4711")
        self.assertEqual(record.einvoice_buyer_reference, "Test User / cost centre 42")
        self.assertEqual(bill.invoice_origin, "PO-ZZ-4711")
        self.assertEqual(bill.einvoice_buyer_reference, "Test User / cost centre 42")
        self.assertEqual(bill.ref, "ZZ-V-2001")

    def test_no_purchase_order_is_linked(self):
        _record, bill = self._import(_invoice_data("fi30_references_discounts.xml"))
        if "purchase_id" in bill._fields:
            self.assertFalse(bill.purchase_id)
        if "purchase_line_id" in bill.invoice_line_ids._fields:
            self.assertFalse(bill.invoice_line_ids.purchase_line_id)

    def test_message_without_references_imports_as_today(self):
        record, bill = self._import(_invoice_data("fi30_no_vat.xml"))
        self.assertFalse(record.einvoice_order_reference)
        self.assertFalse(record.einvoice_buyer_reference)
        self.assertFalse(bill.invoice_origin)
        self.assertFalse(bill.einvoice_buyer_reference)

    def test_order_reference_without_buyer_reference(self):
        record, bill = self._import(_invoice_data("fi30_simple_invoice.xml"))
        self.assertEqual(record.einvoice_order_reference, "PO-2026-118")
        self.assertEqual(bill.invoice_origin, "PO-2026-118")
        self.assertFalse(bill.einvoice_buyer_reference)

    def test_record_fields_are_read_only(self):
        Import = self.env["goodahead.invoice.import"]
        self.assertTrue(Import._fields["einvoice_order_reference"].readonly)
        self.assertTrue(Import._fields["einvoice_buyer_reference"].readonly)
        arch = Import.get_view(
            self.env.ref("goodahead_invoice_import.view_ai_invoice_import_form").id, "form")["arch"]
        self.assertIn('name="einvoice_order_reference"', arch)
        self.assertIn('name="einvoice_buyer_reference"', arch)

    # -- AC-22 -------------------------------------------------------------------

    def test_discounted_rows_keep_the_stated_amount(self):
        _record, bill = self._import(_invoice_data("fi30_references_discounts.xml"))
        lines = self._product_lines(bill)
        self.assertEqual(len(lines), 4)
        stated, rounding_off, amount_only, plain = lines
        # stated percentage reproduces: kept with the gross price
        self.assertEqual((stated.quantity, stated.price_unit, stated.discount), (10, 100.0, 15.0))
        self.assertAlmostEqual(stated.price_subtotal, 850.0)
        # 7 % misses 27.86 by a cent: the worked-out 7.04 % makes it
        self.assertEqual((rounding_off.price_unit, rounding_off.discount), (9.99, 7.04))
        self.assertAlmostEqual(rounding_off.price_subtotal, 27.86)
        # only an amount: 10.00 of 100.00 is 10 %
        self.assertEqual((amount_only.price_unit, amount_only.discount), (25.0, 10.0))
        self.assertAlmostEqual(amount_only.price_subtotal, 90.0)
        # no discount: as today
        self.assertEqual((plain.price_unit, plain.discount), (50.0, 0.0))
        self.assertAlmostEqual(bill.amount_untaxed, 1017.86)
        self.assertFalse(bill.invoice_line_ids.filtered(
            lambda line: line.display_type == "product" and line.name == "Rounding"))

    def test_discount_that_cannot_be_made_goes_net(self):
        data = _invoice_data("fi30_references_discounts.xml")
        # 1 x 1000.00 down to 333.33: at 0.01 % steps the amounts move by 0.10,
        # so no stated or worked-out percentage makes it exactly.
        line = data["lines"][0]
        line.update({"quantity": 1.0, "price_unit": 1000.0, "gross_price": 1000.0,
                     "discount_percent": 66.67, "discount_amount": 666.67, "line_total": 333.33,
                     "tax_amount": 85.0})
        data = self._restate_totals(data)
        _record, bill = self._import(data)
        first = self._product_lines(bill)[0]
        self.assertEqual(first.discount, 0.0)
        self.assertAlmostEqual(first.price_subtotal, 333.33)

    @staticmethod
    def _restate_totals(data):
        untaxed = round(sum(line["line_total"] for line in data["lines"]), 2)
        tax = round(sum(line["tax_amount"] or 0.0 for line in data["lines"]), 2)
        data["tax_subtotals"] = [{"tax_percent": 25.5, "taxable_amount": untaxed,
                                  "tax_amount": tax, "tax_category": "S"}]
        data["totals"] = {"line_extension": untaxed, "tax_exclusive": untaxed,
                          "tax_inclusive": round(untaxed + tax, 2),
                          "payable": round(untaxed + tax, 2)}
        return data

    def test_every_existing_fixture_bill_makes_its_stated_rows(self):
        """Every existing reader fixture (each message): each bill line makes
        its stated row amount, rows without a discount go at their net price
        with no discount as today, and only stated discounts appear."""
        names = sorted(os.path.basename(path) for path in glob.glob(
            os.path.join(BASE_DATA, "fi*.xml")))
        self.assertGreaterEqual(len(names), 15)
        checked = 0
        for name in names:
            with open(os.path.join(BASE_DATA, name), "rb") as handle:
                messages = finvoice.parse(handle.read())
            for message in messages:
                try:
                    data = finvoice.to_invoice_data(message)
                except finvoice.FinvoiceError:
                    continue
                with self.subTest(fixture=name, number=data.get("invoice_number")):
                    record = self.env["goodahead.invoice.import"]._create_for_source(
                        "upload", self.company,
                        [(f"zz-{checked}.xml", b"<zz/>", "application/xml")])
                    result = record._import_from_invoice_data(data, STRICT_POLICY)
                    if result.outcome != "imported":
                        continue
                    checked += 1
                    lines = self._product_lines(result.move)
                    stated = [line for line in data["lines"]
                              if line.get("line_total") is not None]
                    if len(lines) != len(stated):
                        continue
                    for bill_line, row in zip(lines, stated):
                        if "gross_price" not in row:
                            self.assertEqual(bill_line.discount, 0.0)
                        self.assertAlmostEqual(abs(bill_line.price_subtotal),
                                               abs(row["line_total"]), places=2)
        self.assertGreaterEqual(checked, 10)

    # -- the post-import fallback (review F1, F4) ----------------------------------

    def _bill_with_line(self, quantity, price_unit, discount):
        return self.env["account.move"].create({
            "move_type": "in_invoice",
            "partner_id": self.env["res.partner"].search(
                [("name", "=", "Example Supplier Oy")], limit=1).id,
            "journal_id": self.journal.id,
            "invoice_line_ids": [(0, 0, {
                "name": "ZZ row", "quantity": quantity, "price_unit": price_unit,
                "discount": discount, "tax_ids": False,
            })],
        })

    def _keep(self, bill, row):
        record = self.env["goodahead.invoice.import"]._create_for_source(
            "upload", self.company, [("zz-keep.xml", b"<zz/>", "application/xml")])
        record._import_keep_stated_row_amounts({"lines": [row]}, bill)
        return self._product_lines(bill)

    def test_fallback_keeps_the_sign_of_a_negative_row(self):
        """review F4: a negative row whose discount cannot be made goes at
        its signed net price, never flipped positive."""
        bill = self._bill_with_line(1.0, -1000.0, 66.67)
        line = self._keep(bill, {
            "quantity": 1.0, "price_unit": -1000.0, "gross_price": -1000.0,
            "discount_percent": 66.67, "discount_amount": -666.67, "line_total": -333.33,
        })
        self.assertEqual(line.discount, 0.0)
        self.assertAlmostEqual(line.price_unit, -333.33)
        self.assertAlmostEqual(line.price_subtotal, -333.33)

    def test_fallback_goes_one_unit_when_the_net_price_cannot_be_stored(self):
        """review F1: the stored price is 0.80 (2 decimals), so neither the
        stated 5 % nor the net price 0.75847 (stored 0.76) makes 758.47 on
        1000 units: the line goes as one unit at the stated amount, the
        sender's quantity and price kept in its text."""
        bill = self._bill_with_line(1000.0, 0.80, 5.0)
        line = self._keep(bill, {
            "quantity": 1000.0, "price_unit": 0.79839, "gross_price": 0.79839,
            "discount_percent": 5.0, "discount_amount": 39.92, "line_total": 758.47,
        })
        self.assertEqual((line.quantity, line.discount), (1.0, 0.0))
        self.assertAlmostEqual(line.price_subtotal, 758.47)
        self.assertIn("1000 × 0.79839", line.name)

    def test_kept_discount_is_checked_at_the_stored_price(self):
        """review F1: a 5-decimal gross price is stored in 2 decimals; the
        whole import reproduces the stated amount (no Rounding line)."""
        data = _invoice_data("fi30_references_discounts.xml")
        data["lines"][0].update({
            "quantity": 1000.0, "price_unit": 0.79839, "gross_price": 0.79839,
            "discount_percent": 5.0, "discount_amount": 39.92, "line_total": 758.47,
            "tax_amount": 193.41})
        data = self._restate_totals(data)
        _record, bill = self._import(data)
        lines = self._product_lines(bill)
        self.assertEqual(len(lines), 4)
        self.assertAlmostEqual(lines[0].price_subtotal, 758.47)
        self.assertAlmostEqual(bill.amount_untaxed, data["totals"]["tax_exclusive"])

    # -- import compatibility (review G2; AC-23, NFR Import compatibility) ----------

    def _bill_signature(self, data):
        """Import ``data`` inside a savepoint that is rolled back, so the
        same message can be imported twice; returns what the bill holds."""
        signature = {}
        try:
            with self.env.cr.savepoint():
                record = self.env["goodahead.invoice.import"]._create_for_source(
                    "upload", self.company, [("zz-g2.xml", b"<zz/>", "application/xml")])
                result = record._import_from_invoice_data(copy.deepcopy(data), STRICT_POLICY)
                signature["outcome"] = result.outcome
                move = result.move
                if move:
                    signature.update(self._move_signature(move))
                raise _RollBack
        except _RollBack:
            pass
        self.env.invalidate_all()
        return signature

    @staticmethod
    def _move_signature(move):
        lines = move.invoice_line_ids.filtered(lambda line: line.display_type == "product")
        return {
            "type": move.move_type,
            # by name: a vendor created inside the rolled-back savepoint
            # gets a new id the next time
            "partner": move.partner_id.name,
            "ref": move.ref,
            "currency": move.currency_id.name,
            "lines": [
                (line.name, line.quantity, line.price_unit, line.discount,
                 tuple(sorted(line.tax_ids.mapped("name"))), line.account_id.code,
                 line.product_uom_id.name, round(line.price_subtotal, 2))
                for line in lines
            ],
            "untaxed": round(move.amount_untaxed, 2),
            "tax": round(move.amount_tax, 2),
            "total": round(move.amount_total, 2),
        }

    def test_fixtures_without_discounts_give_the_same_bill_as_before(self):
        """review G2: every Finvoice fixture message without a discounted row
        gives the same bill from the reader output captured before this
        feature (base ``tests/data/reader_snapshot.json``) as from today's
        reader - price, quantity, discount, taxes, account, unit, totals.
        Discounted rows are the deliberate exception (spec §1, AC-22)."""
        with open(os.path.join(BASE_DATA, "reader_snapshot.json"), encoding="utf-8") as handle:
            snapshot = json.load(handle)
        compared = 0
        for name, messages in sorted(snapshot.items()):
            with open(os.path.join(BASE_DATA, name), "rb") as handle:
                parsed = finvoice.parse(handle.read())
            for index, before in enumerate(messages):
                if "error" in before:
                    continue
                now = _plain(finvoice.to_invoice_data(parsed[index]))
                if any("gross_price" in line for line in now["lines"]):
                    continue
                with self.subTest(fixture=name, message=index):
                    expected = self._bill_signature(before["data"])
                    self.assertEqual(self._bill_signature(now), expected)
                    if expected.get("outcome") == "imported":
                        compared += 1
        self.assertGreaterEqual(compared, 10)

    def test_fi13_discounted_rows_now_carry_their_discount(self):
        _record, bill = self._import(_invoice_data("fi13_invoice.xml"))
        discounted = self._product_lines(bill).filtered("discount")
        self.assertEqual(len(discounted), 2)
        self.assertEqual(set(discounted.mapped("discount")), {40.0})
        self.assertEqual(sorted(discounted.mapped("price_unit")), [25.0, 50.0])
        self.assertEqual(sorted(discounted.mapped("price_subtotal")), [15.0, 60.0])

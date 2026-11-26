# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Text rows, discounts and trade item numbers in the invoice values
(finvoice content T12; AC-16, AC-17, AC-18, AC-19). Every message built
here is checked against the Finvoice 3.0 schema."""

import unittest
from decimal import ROUND_HALF_UP, Decimal

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.tests import tagged

from odoo.addons.goodahead_einvoice_base.lib import finvoice

from .common import SendApixCommon

CENT = Decimal("0.01")


def _cents(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


@tagged("post_install", "-at_install")
class TestFinvoiceValuesRows(SendApixCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.s24 = cls._make_tax(cls.company, 24.0, "S")
        cls.s255_included = cls._make_tax(cls.company, 25.5, "S", name="ZZ 25.5 % incl.")
        cls.s255_included.price_include_override = "tax_included"
        Product = cls.env["product.product"]
        cls.valid_gtin = Product.create({"name": "ZZ Valid", "barcode": "4006381333931"})
        cls.store_gtin = Product.create({"name": "ZZ Store", "barcode": "2000000000008"})
        cls.bad_gtin = Product.create({"name": "ZZ Bad", "barcode": "40063813339A1"})
        cls.no_gtin = Product.create({"name": "ZZ None"})

    def _line(self, price, quantity=1, discount=0.0, tax=None, **extra):
        return {"name": extra.pop("name", "ZZ line"), "quantity": quantity, "price_unit": price,
                "discount": discount, "tax_ids": [(6, 0, (tax or self.s24).ids)], **extra}

    def _values(self, move):
        values = move._apix_finvoice_values()
        self.assertEqual(finvoice.validate_schema(finvoice.write(values)), [])
        return values

    def _assert_identity(self, row):
        """quantity × price ÷ base quantity − discount = amount, to the cent."""
        discount = row["discount"]["amount"] if row.get("discount") else 0
        gross = _cents(Decimal(str(row["quantity"])) * Decimal(str(row["price_unit"]))
                       / Decimal(str(row["base_quantity"])))
        self.assertEqual(gross - _cents(discount), _cents(row["untaxed"]), row)

    # -- AC-16 ---------------------------------------------------------------------

    def test_headings_and_notes_go_as_text_rows_in_place(self):
        long_note = " ".join(f"word{index:04d}" for index in range(120))  # 1,079 characters
        move = self._make_invoice(lines=[
            {"display_type": "line_section", "name": "Section A"},
            self._line(100.0),
            {"display_type": "line_note", "name": long_note},
            self._line(50.0),
        ])
        values = self._values(move)
        kinds = [row.get("kind", "product") for row in values["lines"]]
        self.assertEqual(kinds, ["text", "product", "text", "product"])
        self.assertEqual(values["lines"][0]["texts"], ["Section A"])
        texts = values["lines"][2]["texts"]
        self.assertGreater(len(texts), 1)
        self.assertTrue(all(len(text) <= 512 for text in texts))
        self.assertEqual(" ".join(texts).split(), long_note.split())
        self.assertAlmostEqual(values["invoice"]["totals"]["rows"], move.amount_untaxed)
        self.assertEqual(len(values["taxes"]), 1)
        rows = finvoice.parse(finvoice.write(values))[0].element.findall("InvoiceRow")
        self.assertEqual([child.tag for child in rows[0]], ["RowFreeText"])

    def test_subsections_go_as_text_rows_too(self):
        """review F2: 19.0's sub-section lines are headings as well.

        Odoo 18.0 has no sub-section line type: every text line type it has
        goes as a text row, and a heading right under a heading (18.0's way
        to write a sub-section) is a text row too."""
        from ..models.account_move_finvoice import TEXT_ROW_TYPES

        display_types = dict(self.env["account.move.line"]._fields["display_type"].selection)
        self.assertNotIn("line_subsection", display_types)
        self.assertEqual(
            set(TEXT_ROW_TYPES), {"line_section", "line_note"} & set(display_types))
        move = self._make_invoice(lines=[
            {"display_type": "line_section", "name": "Section A"},
            {"display_type": "line_section", "name": "Sub-section A.1"},
            self._line(100.0),
        ])
        values = self._values(move)
        self.assertEqual([row.get("kind", "product") for row in values["lines"]],
                         ["text", "text", "product"])
        self.assertEqual(values["lines"][1]["texts"], ["Sub-section A.1"])
        self.assertEqual(len(values["taxes"]), 1)

    # -- AC-17 ---------------------------------------------------------------------

    def test_discount_goes_as_price_percent_and_amount(self):
        move = self._make_invoice(lines=[self._line(100.0, quantity=10, discount=15.0)])
        row = self._values(move)["lines"][0]
        self.assertAlmostEqual(row["price_unit"], 100.0)
        self.assertEqual(row["discount"], {"percent": 15.0, "amount": Decimal("150.00")})
        self.assertAlmostEqual(row["untaxed"], 850.0)
        self.assertAlmostEqual(row["tax"], 204.0)
        self._assert_identity(row)

    # -- AC-18 ---------------------------------------------------------------------

    def test_price_included_tax(self):
        move = self._make_invoice(lines=[self._line(100.0, quantity=10, discount=15.0,
                                                    tax=self.s255_included)])
        row = self._values(move)["lines"][0]
        self.assertAlmostEqual(row["price_unit"], 79.68, places=2)
        self.assertEqual(row["discount"]["percent"], 15.0)
        self.assertAlmostEqual(row["untaxed"], move.amount_untaxed)
        self._assert_identity(row)

    def test_hundred_percent(self):
        move = self._make_invoice(lines=[self._line(100.0), self._line(12.5, quantity=4,
                                                                        discount=100.0)])
        row = self._values(move)["lines"][1]
        self.assertEqual(row["discount"], {"percent": 100.0, "amount": Decimal("50.00")})
        self.assertAlmostEqual(row["untaxed"], 0.0)
        self._assert_identity(row)

    def test_rounding_amount_wins(self):
        move = self._make_invoice(lines=[self._line(9.99, quantity=3, discount=7.0),
                                         self._line(0.333, quantity=7, discount=33.33)])
        for row in self._values(move)["lines"]:
            self._assert_identity(row)

    def test_credit_note_discount(self):
        move = self._make_invoice(lines=[self._line(50.0, quantity=2, discount=20.0)],
                                  move_type="out_refund")
        values = self._values(move)
        row = values["lines"][0]
        self.assertEqual(row["discount"], {"percent": 20.0, "amount": Decimal("20.00")})
        element = finvoice.parse(finvoice.write(values))[0].element.find("InvoiceRow")
        self.assertEqual(element.findtext("RowDiscountAmount"), "-20,00")
        self.assertEqual(element.findtext("UnitPriceAmount"), "50,00")

    def test_surcharge_and_undiscounted_lines_go_net(self):
        move = self._make_invoice(lines=[self._line(100.0, discount=-5.0), self._line(80.0)])
        rows = self._values(move)["lines"]
        self.assertIsNone(rows[0]["discount"])
        self.assertAlmostEqual(rows[0]["price_unit"], 105.0)
        self.assertIsNone(rows[1]["discount"])
        self.assertAlmostEqual(rows[1]["price_unit"], 80.0)

    def test_per_hundred_price_with_discount(self):
        move = self._make_invoice(lines=[{
            **self._line(40.0, quantity=2.5, discount=10.0),
            "product_uom_id": self.unit_hundred.id,
        }])
        row = self._values(move)["lines"][0]
        self.assertEqual(row["base_quantity"], 100.0)
        self.assertAlmostEqual(row["quantity"], 250.0)
        self.assertAlmostEqual(row["price_unit"], 40.0)
        self.assertEqual(row["discount"]["amount"], Decimal("10.00"))
        self._assert_identity(row)

    # -- AC-19 ---------------------------------------------------------------------

    def test_only_a_valid_barcode_is_sent(self):
        move = self._make_invoice(lines=[
            self._line(10.0, product_id=product.id, name=product.name)
            for product in (self.valid_gtin, self.store_gtin, self.bad_gtin, self.no_gtin)
        ])
        rows = self._values(move)["lines"]
        self.assertEqual([row["gtin"] for row in rows], ["4006381333931", None, None, None])

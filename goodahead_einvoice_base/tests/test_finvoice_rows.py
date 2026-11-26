# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Tests of the shared row arithmetic (finvoice content T04; AC-16, AC-17,
AC-18, AC-22; NFR Amount consistency).

Runs without the ERP (``python -I -m unittest discover -s
goodahead/goodahead_einvoice_base/tests -t goodahead``) and inside the ERP's
test runner.
"""

import random
import unittest
from decimal import ROUND_HALF_UP, Decimal

try:  # inside the ERP test runner
    from odoo.tests import BaseCase as _TestCase
    from odoo.tests import tagged
except ImportError:  # plain unittest, no ERP installed
    _TestCase = unittest.TestCase

    def tagged(*_tags):
        return lambda cls: cls

from ..lib.finvoice import rows

D = Decimal
CENT = D("0.01")


def _cents(value):
    return D(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def _identity_holds(quantity, base_quantity, untaxed, discount):
    gross_amount = _cents(D(str(quantity)) * discount["gross_price"] / D(str(base_quantity)))
    return gross_amount - discount["amount"] == _cents(untaxed)


@tagged("post_install", "-at_install")
class TestDiscountRow(_TestCase):
    def test_ac17_example(self):
        discount = rows.discount_row(10, 100.0, 1, 15.0, 850.0)
        self.assertEqual(discount, {"gross_price": D("100"), "percent": D("15"),
                                    "amount": D("150.00")})

    def test_amount_wins_over_the_percentage(self):
        # 3 x 9.99 = 29.97; 7 % of it is 2.10, but the line's own amount is
        # 27.86 (rounded per unit): the discount amount makes the row add up.
        discount = rows.discount_row(3, 9.99, 1, 7.0, 27.86)
        self.assertEqual(discount["amount"], D("2.11"))
        self.assertEqual(discount["percent"], D("7"))
        self.assertTrue(_identity_holds(3, 1, 27.86, discount))

    def test_hundred_percent(self):
        discount = rows.discount_row(4, 12.5, 1, 100.0, 0.0)
        self.assertEqual(discount, {"gross_price": D("12.5"), "percent": D("100"),
                                    "amount": D("50.00")})

    def test_per_several_units(self):
        # 250 pieces at 40.00 per 100, 10 % off: 100.00 - 10.00 = 90.00.
        discount = rows.discount_row(250, 40.0, 100, 10.0, 90.0)
        self.assertEqual(discount["amount"], D("10.00"))
        self.assertTrue(_identity_holds(250, 100, 90.0, discount))

    def test_no_or_negative_discount_goes_net(self):
        self.assertIsNone(rows.discount_row(1, 100.0, 1, 0.0, 100.0))
        self.assertIsNone(rows.discount_row(1, 100.0, 1, None, 100.0))
        self.assertIsNone(rows.discount_row(1, 100.0, 1, -5.0, 105.0))

    def test_gross_price_needing_six_decimals_goes_net(self):
        self.assertIsNone(rows.discount_row(1, 1.234567, 1, 10.0, 1.11))

    def test_five_decimals_are_statable(self):
        self.assertIsNotNone(rows.discount_row(1, 1.23456, 1, 10.0, 1.11))

    def test_rounded_gross_below_the_amount_goes_net(self):
        # A discount so small that the gross row rounds below the line amount.
        self.assertIsNone(rows.discount_row(1, 10.0, 1, 0.001, 10.01))

    def test_zero_quantity_goes_net(self):
        self.assertIsNone(rows.discount_row(0, 10.0, 1, 10.0, 0.0))

    def test_amounts_are_not_signed_here(self):
        # A credit note's values are positive; the writer negates them.
        discount = rows.discount_row(2, 50.0, 1, 20.0, 80.0)
        self.assertEqual(discount["amount"], D("20.00"))

    def test_property_500_random_discounted_lines(self):
        generator = random.Random(4711)
        failures = []
        stated = 0
        for _index in range(600):
            quantity = D(generator.choice(["1", "2", "3", "7", "12.5", "0.333", "1000"]))
            base_quantity = D(generator.choice(["1", "1", "1", "10", "100"]))
            price = D(generator.randint(1, 10_000_000)) / D(generator.choice([100, 1000, 100000]))
            percent = D(generator.randint(1, 10000)) / D(100)
            # A price-included 24 % or 25.5 % tax: the gross without VAT.
            divisor = generator.choice([D(1), D("1.24"), D("1.255")])
            gross = (price / divisor).quantize(D("0.00001"), rounding=ROUND_HALF_UP)
            untaxed = _cents(quantity * price / divisor / base_quantity
                             * (1 - percent / 100))
            discount = rows.discount_row(quantity, gross, base_quantity, percent, untaxed)
            if discount is None:
                continue
            stated += 1
            if not _identity_holds(quantity, base_quantity, untaxed, discount):
                failures.append((quantity, gross, base_quantity, percent, untaxed, discount))
            if discount["amount"] < 0:
                failures.append(("negative", quantity, gross, percent, discount))
        self.assertEqual(failures, [])
        self.assertGreaterEqual(stated, 500)


@tagged("post_install", "-at_install")
class TestImportDiscount(_TestCase):
    def test_reproduces(self):
        self.assertTrue(rows.reproduces(10, 100.0, 1, 15.0, 850.0, digits=2))
        self.assertFalse(rows.reproduces(10, 100.0, 1, 15.0, 850.01, digits=2))
        self.assertTrue(rows.reproduces(250, 40.0, 100, 10.0, 90.0, digits=2))

    def test_stated_percentage_that_reproduces(self):
        self.assertEqual(rows.import_discount(10, 100.0, 1, 15.0, 150.0, 850.0, digits=2),
                         {"price": D("100.0"), "percent": D("15.00")})

    def test_percentage_missing_by_rounding_is_worked_out(self):
        # 3 x 9.99 = 29.97, stated 7 % (27.87) but amount 27.86: 7.04 % makes it.
        self.assertFalse(rows.reproduces(3, 9.99, 1, 7.0, 27.86, digits=2))
        result = rows.import_discount(3, 9.99, 1, 7.0, 2.11, 27.86, digits=2)
        self.assertEqual(result, {"price": D("9.99"), "percent": D("7.04")})
        self.assertTrue(rows.reproduces(3, 9.99, 1, result["percent"], 27.86, digits=2))

    def test_amount_only_discount(self):
        result = rows.import_discount(10, 100.0, 1, None, 150.0, 850.0, digits=2)
        self.assertEqual(result, {"price": D("100.0"), "percent": D("15.00")})

    def test_row_amount_derived_from_the_discount_amount(self):
        result = rows.import_discount(10, 100.0, 1, None, 150.0, None, digits=2)
        self.assertEqual(result, {"price": D("100.0"), "percent": D("15.00")})

    def test_nothing_reproduces_goes_net(self):
        # 7 x 1.00 = 7.00 down to 3.33: at whole percents 52 % gives 3.36 and
        # 53 % gives 3.29, so neither the stated 50 % nor a worked-out one makes it.
        self.assertIsNone(rows.import_discount(7, 1.0, 1, 50.0, 3.67, 3.33, digits=0))
        self.assertIsNone(rows.import_discount(7, 1.0, 1, None, None, 3.33, digits=0))

    def test_surcharge_or_zero_goes_net(self):
        self.assertIsNone(rows.import_discount(1, 100.0, 1, -5.0, -5.0, 105.0, digits=2))
        self.assertIsNone(rows.import_discount(1, 100.0, 1, 0.0, 0.0, 100.0, digits=2))
        self.assertIsNone(rows.import_discount(0, 100.0, 1, 10.0, 0.0, 0.0, digits=2))

    def test_hundred_percent(self):
        self.assertEqual(rows.import_discount(2, 10.0, 1, 100.0, 20.0, 0.0, digits=2),
                         {"price": D("10.0"), "percent": D("100.00")})

    def test_credit_note_signs(self):
        # Credit notes read with negative quantities and amounts.
        self.assertEqual(rows.import_discount(-10, 100.0, 1, 15.0, -150.0, -850.0, digits=2),
                         {"price": D("100.0"), "percent": D("15.00")})


@tagged("post_install", "-at_install")
class TestTextRowTexts(_TestCase):
    def test_empty(self):
        self.assertEqual(rows.text_row_texts(None), [])
        self.assertEqual(rows.text_row_texts("  \n "), [])

    def test_short(self):
        self.assertEqual(rows.text_row_texts("  Section A  "), ["Section A"])

    def test_long_note_split_at_word_boundaries(self):
        words = [f"word{index:04d}" for index in range(140)]  # 1,399 characters
        long_note = "  ".join(words[:70]) + "\n" + " ".join(words[70:])
        texts = rows.text_row_texts(long_note)
        self.assertGreater(len(texts), 1)
        for chunk in texts:
            self.assertLessEqual(len(chunk), rows.TEXT_MAX)
            self.assertEqual(chunk, chunk.strip())
        self.assertEqual(" ".join(texts).split(), long_note.split())

    def test_1300_characters_give_three_texts(self):
        note = " ".join(["abcdefghi"] * 130)  # 1,299 characters
        texts = rows.text_row_texts(note)
        self.assertEqual(len(texts), 3)
        self.assertEqual(" ".join(texts), note)

    def test_word_longer_than_a_text_is_cut_not_lost(self):
        word = "x" * 1100
        texts = rows.text_row_texts("start " + word + " end")
        self.assertTrue(all(len(chunk) <= rows.TEXT_MAX for chunk in texts))
        self.assertEqual("".join(texts).replace(" ", ""), "start" + word + "end")


if __name__ == "__main__":
    unittest.main()

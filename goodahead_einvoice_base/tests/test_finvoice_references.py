# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Tests of the pure order-reference and trade-item-number rules (finvoice
content T03; AC-10, AC-11, AC-19, AC-27).

Runs without the ERP (``python -I -m unittest discover -s
goodahead/goodahead_einvoice_base/tests -t goodahead``) and inside the ERP's
test runner. Every number is invented.
"""

import unittest

try:  # inside the ERP test runner
    from odoo.tests import BaseCase as _TestCase
    from odoo.tests import tagged
except ImportError:  # plain unittest, no ERP installed
    _TestCase = unittest.TestCase

    def tagged(*_tags):
        return lambda cls: cls

from ..lib.finvoice import gtin, references


def _with_check_digit(body):
    """GS1 mod-10: weights 3, 1, 3, ... from the right of the body."""
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return body + str((10 - total % 10) % 10)


@tagged("post_install", "-at_install")
class TestOurOrderNumber(_TestCase):
    def test_source_document_as_written(self):
        self.assertEqual(references.our_order_number("S00012"), ("S00012", None))
        self.assertEqual(references.our_order_number("  S00012, S00013 "),
                         ("S00012, S00013", None))

    def test_empty(self):
        self.assertEqual(references.our_order_number(None), (None, None))
        self.assertEqual(references.our_order_number(""), (None, None))
        self.assertEqual(references.our_order_number("   "), (None, None))

    def test_70_fits_71_is_left_out(self):
        self.assertEqual(references.MAX_REFERENCE, 70)
        self.assertEqual(references.our_order_number("S" * 70), ("S" * 70, None))
        self.assertEqual(references.our_order_number("S" * 71), (None, "too_long"))


@tagged("post_install", "-at_install")
class TestCustomerOrderNumber(_TestCase):
    def test_no_echo(self):
        self.assertEqual(references.customer_order_number("PO-4711", "S00012"),
                         ("PO-4711", None))

    def test_single_echo_of_our_number(self):
        self.assertEqual(references.customer_order_number("S00012", "S00012"), (None, None))

    def test_grouped_invoice(self):
        self.assertEqual(
            references.customer_order_number("PO-1, S00012, PO-2,S00013", "S00012, S00013"),
            ("PO-1, PO-2", None),
        )

    def test_echo_with_spaces_around_commas(self):
        self.assertEqual(references.customer_order_number(" PO-1 ,  S00012 ", "S00012"),
                         ("PO-1", None))

    def test_comparison_is_case_sensitive(self):
        self.assertEqual(references.customer_order_number("s00012", "S00012"),
                         ("s00012", None))

    def test_without_source_document(self):
        self.assertEqual(references.customer_order_number("PO-1", None), ("PO-1", None))
        self.assertEqual(references.customer_order_number("PO-1", False), ("PO-1", None))

    def test_empty(self):
        self.assertEqual(references.customer_order_number(None, "S00012"), (None, None))
        self.assertEqual(references.customer_order_number(" , ", "S00012"), (None, None))

    def test_70_fits_71_is_left_out(self):
        self.assertEqual(references.customer_order_number("P" * 70, "S1"), ("P" * 70, None))
        self.assertEqual(references.customer_order_number("P" * 71, "S1"), (None, "too_long"))

    def test_length_is_measured_after_the_echo_is_removed(self):
        reference = "P" * 60 + ", S00000000012"
        self.assertEqual(references.customer_order_number(reference, "S00000000012"),
                         ("P" * 60, None))


@tagged("post_install", "-at_install")
class TestSellerReferenceAllowed(_TestCase):
    # 1232 passes the Finnish 7-3-1 check (body 123, check digit 2).
    NUMERIC_ORDER = "1232"
    VALID_PAYMENT_REFERENCE = "1009"

    def test_fixtures_are_what_they_claim(self):
        from ..lib.finvoice import values
        self.assertTrue(values.payment_reference_is_valid(self.NUMERIC_ORDER))
        self.assertTrue(values.payment_reference_is_valid(self.VALID_PAYMENT_REFERENCE))

    def test_numeric_order_number_without_a_payment_reference_is_withheld(self):
        self.assertFalse(references.seller_reference_allowed(self.NUMERIC_ORDER, None))
        self.assertFalse(references.seller_reference_allowed(self.NUMERIC_ORDER, ""))
        self.assertFalse(references.seller_reference_allowed(self.NUMERIC_ORDER, "INV/1"))

    def test_numeric_order_number_with_a_valid_payment_reference(self):
        self.assertTrue(references.seller_reference_allowed(
            self.NUMERIC_ORDER, self.VALID_PAYMENT_REFERENCE))

    def test_an_order_number_that_is_no_payment_reference(self):
        self.assertTrue(references.seller_reference_allowed("S00012", None))
        self.assertTrue(references.seller_reference_allowed("1233", None))

    def test_no_order_number(self):
        self.assertTrue(references.seller_reference_allowed(None, None))


@tagged("post_install", "-at_install")
class TestGtin(_TestCase):
    def test_scheme(self):
        self.assertEqual(gtin.GTIN_SCHEME, "0160")

    def test_valid_numbers_of_every_length(self):
        for value in ("96385074", "036000291452", "4006381333931",
                      _with_check_digit("1061414100041")):
            with self.subTest(value=value):
                self.assertTrue(gtin.is_valid_gtin(value))

    def test_wrong_check_digit(self):
        for value in ("96385075", "036000291453", "4006381333932"):
            with self.subTest(value=value):
                self.assertFalse(gtin.is_valid_gtin(value))

    def test_wrong_length_or_letters(self):
        for value in ("1234567", "123456789", _with_check_digit("1234567890"),
                      "40063813339A1", "4006 381333931", " 4006381333931", "", None, False):
            with self.subTest(value=value):
                self.assertFalse(gtin.is_valid_gtin(value))

    def test_restricted_circulation_numbers(self):
        bodies = (
            "020000000001", "040000000001",
            *(f"2{d}0000000001" for d in range(10)),
            "0020000000001", "0040000000001", "0200000000001", "0290000000001",
            "20000000001", "40000000001",
            "0000001", "2000001",
        )
        for body in bodies:
            value = _with_check_digit(body)
            with self.subTest(value=value):
                self.assertFalse(gtin.is_valid_gtin(value))

    def test_neighbours_of_restricted_prefixes_stay_valid(self):
        for body in ("030000000001", "190000000001", "0030000000001", "0190000000001",
                     "30000000001", "1000001", "3000001"):
            value = _with_check_digit(body)
            with self.subTest(value=value):
                self.assertTrue(gtin.is_valid_gtin(value))


if __name__ == "__main__":
    unittest.main()

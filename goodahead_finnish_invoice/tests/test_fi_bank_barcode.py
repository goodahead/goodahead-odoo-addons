# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Finnish bank bar code, versions 4 and 5.

Pure Python. Runs with plain unittest, without the ERP:

    python -m unittest discover -s goodahead_finnish_invoice/tests -t goodahead_finnish_invoice

and under the ERP's runner. Positions follow the bank bar code guideline of
Finance Finland (the module docstring of ``lib/fi_bank_barcode.py``).
"""

import unittest
from datetime import date

if __name__.startswith("odoo.addons."):
    from odoo.tests import BaseCase, tagged

    from odoo.addons.goodahead_finnish_invoice.lib import fi_bank_barcode
else:  # plain unittest, no ERP
    from lib import fi_bank_barcode

    BaseCase = unittest.TestCase

    def tagged(*_tags):
        return lambda cls: cls


IBAN = "FI2112345600000785"  # the well-known example account, not a real one
DUE = date(2026, 12, 31)


@tagged("post_install", "-at_install")
class TestBankBarcodeVersion4(BaseCase):
    def test_guideline_example(self):
        """The guideline's worked version 4 example, digit for digit."""
        barcode = fi_bank_barcode.build_barcode(
            "FI7944052020036082", 4883.15, "868516259619897", date(2010, 6, 12)
        )
        self.assertEqual(barcode, "479440520200360820048831500000000868516259619897100612")

    def test_fields_in_their_positions(self):
        barcode = fi_bank_barcode.build_barcode(IBAN, 1234.5, "10001235", DUE)
        self.assertEqual(len(barcode), fi_bank_barcode.BARCODE_LENGTH)
        self.assertEqual(barcode[0], "4")
        self.assertEqual(barcode[1:17], "2112345600000785")  # IBAN without FI
        self.assertEqual(barcode[17:23], "001234")  # euros
        self.assertEqual(barcode[23:25], "50")  # cents
        self.assertEqual(barcode[25:28], "000")  # reserve
        self.assertEqual(barcode[28:48], "10001235".rjust(20, "0"))  # reference
        self.assertEqual(barcode[48:54], "261231")  # due date YYMMDD

    def test_no_due_date_is_six_zeros(self):
        barcode = fi_bank_barcode.build_barcode(IBAN, 10, "10001235", None)
        self.assertEqual(barcode[-6:], fi_bank_barcode.NO_DUE_DATE)

    def test_cents_are_rounded_not_truncated(self):
        barcode = fi_bank_barcode.build_barcode(IBAN, 0.29, "10001235", DUE)
        self.assertEqual(barcode[17:25], "00000029")


@tagged("post_install", "-at_install")
class TestBankBarcodeVersion5(BaseCase):
    REFERENCE = "RF18539007547034"  # the ISO 11649 example creditor reference

    def test_fields_in_their_positions(self):
        barcode = fi_bank_barcode.build_barcode(IBAN, 482.99, self.REFERENCE, DUE)
        self.assertEqual(len(barcode), fi_bank_barcode.BARCODE_LENGTH)
        self.assertEqual(barcode[0], "5")
        self.assertEqual(barcode[1:17], "2112345600000785")  # IBAN without FI
        self.assertEqual(barcode[17:23], "000482")  # euros
        self.assertEqual(barcode[23:25], "99")  # cents
        self.assertEqual(barcode[25:27], "18")  # RF check digits
        self.assertEqual(barcode[27:48], "539007547034".rjust(21, "0"))  # reference
        self.assertEqual(barcode[48:54], "261231")  # due date YYMMDD

    def test_whole_code(self):
        barcode = fi_bank_barcode.build_barcode(IBAN, 482.99, self.REFERENCE, DUE)
        self.assertEqual(barcode, "521123456000007850004829918000000000539007547034261231")


@tagged("post_install", "-at_install")
class TestBarcodeInputError(BaseCase):
    def test_valid_inputs_have_no_error(self):
        self.assertIsNone(fi_bank_barcode.barcode_input_error(IBAN, 10.0, "10001235"))
        self.assertIsNone(fi_bank_barcode.barcode_input_error(IBAN, 10.0, "RF18539007547034"))
        self.assertIsNone(fi_bank_barcode.barcode_input_error(IBAN, fi_bank_barcode.BARCODE_MAX_AMOUNT, "1232"))

    def test_each_reason(self):
        cases = [
            ("", 10.0, "10001235", "no recipient bank account"),
            ("SE4550000000058398257466", 10.0, "10001235", "recipient account is not a Finnish IBAN"),
            ("FI21123456", 10.0, "10001235", "recipient account is not a Finnish IBAN"),
            (IBAN, -1.0, "10001235", "amount is negative"),
            (IBAN, 1000000.0, "10001235", "amount exceeds the six digits the bar code allows"),
            (IBAN, 10.0, "", "no payment reference"),
            (IBAN, 10.0, "RFXX539007547034", "malformed RF reference RFXX539007547034"),
            (IBAN, 10.0, "RF18" + "1" * 22, "RF reference RF18%s is too long" % ("1" * 22)),
            (IBAN, 10.0, "SO-7781", "reference SO-7781 is not a Finnish reference number"),
            (IBAN, 10.0, "1" * 21, "reference %s is too long" % ("1" * 21)),
        ]
        for iban, amount, reference, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(fi_bank_barcode.barcode_input_error(iban, amount, reference), reason)

    def test_reason_never_carries_the_account(self):
        reason = fi_bank_barcode.barcode_input_error("SE4550000000058398257466", 10.0, "10001235")
        self.assertNotIn("5839825", reason)

    def test_amount_above_the_maximum_gets_no_barcode(self):
        """The guard after validation: a seventh euro digit breaks the length."""
        self.assertFalse(fi_bank_barcode.build_barcode(IBAN, 1000000.0, "10001235", DUE))


if __name__ == "__main__":
    unittest.main()

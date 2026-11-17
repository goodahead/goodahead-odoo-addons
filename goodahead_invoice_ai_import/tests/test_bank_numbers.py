# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Bank-number checksums, and the limits of what they can protect.

The IBAN mod-97 check is shape-gated, so US numbers pass through it unjudged;
`aba_checksum_fails` guards US routing numbers instead. A US account number has
no checksum at all, which is stated here as a test.
"""

from odoo.tests import BaseCase, tagged

from odoo.addons.goodahead_invoice_ai_import.lib.bank_numbers import (
    aba_checksum_fails,
    iban_checksum_fails,
    payee_account_number,
    payee_institution_id,
)

# Real, published routing numbers — these identify banks, not accounts.
VALID_ROUTING = ["021000021", "111000025", "026009593", "011401533"]
VALID_IBANS = ["GB33BUKB20201555555555", "FI1640001000000017", "DE89370400440532013000"]


@tagged("post_install", "-at_install")
class TestBankNumbers(BaseCase):
    # ------------------------------------------------------------------
    # The IBAN guard, and why US numbers survive it
    # ------------------------------------------------------------------

    def test_iban_checksum_accepts_valid_ibans(self):
        for iban in VALID_IBANS:
            self.assertFalse(iban_checksum_fails(iban), iban)

    def test_iban_checksum_catches_a_misread_digit(self):
        # A valid IBAN with one digit too many.
        self.assertTrue(iban_checksum_fails("FI16400010000000017"))

    def test_iban_checksum_ignores_formatting(self):
        self.assertFalse(iban_checksum_fails("FI16 4000 1000 0000 17"))
        self.assertFalse(iban_checksum_fails("FI16400010000000 17"))

    def test_the_iban_guard_is_inert_for_us_numbers(self):
        """No US number is rejected by the IBAN check."""
        us_numbers = [
            *VALID_ROUTING,
            "123456789",       # routing-shaped, bad ABA checksum
            "1234567890",      # account number
            "000123456789",    # account number with leading zeros
            "12-3456789",      # hyphenated
            "4521 8899 0021",  # spaced
            "987654320",
        ]
        for number in us_numbers:
            self.assertFalse(
                iban_checksum_fails(number),
                f"{number} was treated as a malformed IBAN; it is a US number",
            )

    def test_iban_guard_declines_empty_input(self):
        self.assertFalse(iban_checksum_fails(None))
        self.assertFalse(iban_checksum_fails(""))

    # ------------------------------------------------------------------
    # The ABA guard — the US counterpart
    # ------------------------------------------------------------------

    def test_aba_checksum_accepts_real_routing_numbers(self):
        for routing in VALID_ROUTING:
            self.assertFalse(aba_checksum_fails(routing), routing)

    def test_aba_checksum_catches_a_bad_routing_number(self):
        self.assertTrue(aba_checksum_fails("123456789"))

    def test_aba_checksum_catches_a_transposed_digit(self):
        """Two swapped digits are caught; the unswapped number is the control."""
        self.assertFalse(aba_checksum_fails("021000021"))
        self.assertTrue(aba_checksum_fails("021000012"))

    def test_aba_guard_is_inert_for_anything_not_nine_digits(self):
        """Shape-gated like the IBAN check: non-routing values are not judged."""
        for number in [*VALID_IBANS, "1234567890", "12345678", "", None]:
            self.assertFalse(aba_checksum_fails(number), repr(number))

    def test_a_us_account_number_cannot_be_verified(self):
        """A US account number carries no check digits, so it cannot be verified.

        A clean import of a US bill means only the routing number was checked.
        """
        for number in ["1234567890", "1234567891", "0", "99999999999999"]:
            self.assertFalse(aba_checksum_fails(number))
            self.assertFalse(iban_checksum_fails(number))

    # ------------------------------------------------------------------
    # Which field holds the account
    # ------------------------------------------------------------------

    def test_payee_account_prefers_iban_then_account_number(self):
        self.assertEqual(
            payee_account_number({"iban": "FI1640001000000017"}),
            "FI1640001000000017",
        )
        self.assertEqual(
            payee_account_number({"account_number": "1234567890"}), "1234567890"
        )
        # A foreign vendor who states both: the IBAN is globally unambiguous.
        self.assertEqual(
            payee_account_number(
                {"iban": "FI1640001000000017", "account_number": "1234567890"}
            ),
            "FI1640001000000017",
        )

    def test_payee_institution_prefers_bic_then_routing(self):
        self.assertEqual(payee_institution_id({"bic": "DABAFIHH"}), "DABAFIHH")
        self.assertEqual(
            payee_institution_id({"routing_number": "021000021"}), "021000021"
        )

    def test_payee_accessors_tolerate_nothing_and_rubbish(self):
        for seller in [{}, None, "not a dict", {"iban": "", "account_number": "  "}]:
            self.assertIsNone(payee_account_number(seller))
            self.assertIsNone(payee_institution_id(seller))

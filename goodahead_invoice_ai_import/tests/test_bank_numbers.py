# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Bank-number checksums, and the limits of what they can protect.

🔴 THE QUESTION THIS FILE ANSWERS. The module's most valuable guard is that a
misread number never becomes a vendor bank account. That guard was built around
the IBAN mod-97 check, and the US deployment's vendors do not have IBANs. The obvious
fear is that the guard would now reject every legitimate US account — a silent,
total failure of the thing that matters most.

It does not, and `test_the_iban_guard_is_inert_for_us_numbers` is the proof: the
mod-97 check is SHAPE-GATED, so a number that is not IBAN-shaped is passed
through rather than judged. The real gap was the mirror image — US numbers were
not rejected, they were simply UNGUARDED — and `aba_checksum_fails` closes it.

⚠️ And one thing no test can fix, asserted here so it stays visible: a US
ACCOUNT number has no checksum. `test_a_us_account_number_cannot_be_verified`
exists to state that in code.
"""

from odoo.tests import BaseCase, tagged

from odoo.addons.goodahead_invoice_import.lib.bank_numbers import (
    aba_checksum_fails,
    iban_checksum_fails,
    payee_account_number,
    payee_institution_id,
)

# Real, published routing numbers — these identify banks, not accounts.
VALID_ROUTING = ["021000021", "111000025", "026009593", "011401533"]
VALID_IBANS = ["GB33BUKB20201555555555", "FI2912345600004521", "DE89370400440532013000"]


@tagged("post_install", "-at_install")
class TestBankNumbers(BaseCase):
    # ------------------------------------------------------------------
    # The IBAN guard, and why US numbers survive it
    # ------------------------------------------------------------------

    def test_iban_checksum_accepts_valid_ibans(self):
        for iban in VALID_IBANS:
            self.assertFalse(iban_checksum_fails(iban), iban)

    def test_iban_checksum_catches_a_misread_digit(self):
        # A valid number read with one digit too many — the case that started it.
        self.assertTrue(iban_checksum_fails("FI29123456000004521"))

    def test_iban_checksum_ignores_formatting(self):
        self.assertFalse(iban_checksum_fails("FI29 1234 5600 0045 21"))
        self.assertFalse(iban_checksum_fails("FI29123456000045 21"))

    def test_the_iban_guard_is_inert_for_us_numbers(self):
        """🔴 The headline: no US number is rejected by the IBAN check.

        If this ever fails, every US vendor's bank details are being thrown
        away and the bills are silently stating no account.
        """
        us_numbers = [
            *VALID_ROUTING,
            "123456789",       # routing-shaped, bad ABA checksum
            "1234567890",      # account number
            "000123456789",    # account number with leading zeros
            "12-3456789",      # hyphenated
            "4521 8899 0021",  # spaced
            "630830900",
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
        """The error a human or a model actually makes: two digits swapped.

        `021000021` -> `021000012`. The control is the line above it: the
        unswapped number must pass, or this proves nothing.
        """
        self.assertFalse(aba_checksum_fails("021000021"))
        self.assertTrue(aba_checksum_fails("021000012"))

    def test_aba_guard_is_inert_for_anything_not_nine_digits(self):
        """Shape-gated exactly like its IBAN twin: an IBAN, a longer account
        number or an empty value is not a routing number and is not judged."""
        for number in [*VALID_IBANS, "1234567890", "12345678", "", None]:
            self.assertFalse(aba_checksum_fails(number), repr(number))

    def test_a_us_account_number_cannot_be_verified(self):
        """🔴 Written down as a test because it is a limit, not a gap.

        A US account number carries no check digits. `1234567890` and
        `1234567891` are equally plausible, and no code can tell them apart. So
        a clean import of a US bill does NOT mean the account number was
        verified — only that the routing number was. Nobody should read the
        absence of a warning as a guarantee.
        """
        for number in ["1234567890", "1234567891", "0", "99999999999999"]:
            self.assertFalse(aba_checksum_fails(number))
            self.assertFalse(iban_checksum_fails(number))

    # ------------------------------------------------------------------
    # Which field holds the account
    # ------------------------------------------------------------------

    def test_payee_account_prefers_iban_then_account_number(self):
        self.assertEqual(
            payee_account_number({"iban": "FI2912345600004521"}),
            "FI2912345600004521",
        )
        self.assertEqual(
            payee_account_number({"account_number": "1234567890"}), "1234567890"
        )
        # A foreign vendor who states both: the IBAN is globally unambiguous.
        self.assertEqual(
            payee_account_number(
                {"iban": "FI2912345600004521", "account_number": "1234567890"}
            ),
            "FI2912345600004521",
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

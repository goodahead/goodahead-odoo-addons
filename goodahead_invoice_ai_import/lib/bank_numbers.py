# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Bank-number shapes, checksums, and which field holds the payee's account.

An IBAN's mod-97 covers the whole number. An ABA checksum covers only the US
routing number; a US account number has no checksum and cannot be verified.
"""

import re

# Country code, two check digits, 11-30 alphanumerics: the shape of an IBAN.
IBAN_SHAPE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")

# A US ABA routing transit number: exactly nine digits.
ABA_SHAPE = re.compile(r"^\d{9}$")


def sanitize(account_number):
    """Strip punctuation and whitespace, uppercase (ORM-free copy of Odoo's
    `sanitize_account_number`)."""
    if not account_number:
        return ""
    return re.sub(r"\W+", "", str(account_number)).upper()


def iban_checksum_fails(account_number):
    """True when ``account_number`` looks like an IBAN but fails ISO 13616
    mod-97. Numbers that are not IBAN-shaped (e.g. US numbers) return False.
    """
    number = sanitize(account_number)
    if not number or not IBAN_SHAPE.match(number):
        return False
    rearranged = number[4:] + number[:4]
    return int("".join(str(int(char, 36)) for char in rearranged)) % 97 != 1


def aba_checksum_fails(routing_number):
    """True when ``routing_number`` is nine digits and fails the ABA checksum:
    3(d1+d4+d7) + 7(d2+d5+d8) + (d3+d6+d9) must be divisible by 10.
    """
    number = sanitize(routing_number)
    if not number or not ABA_SHAPE.match(number):
        return False
    digits = [int(d) for d in number]
    total = (
        3 * (digits[0] + digits[3] + digits[6])
        + 7 * (digits[1] + digits[4] + digits[7])
        + (digits[2] + digits[5] + digits[8])
    )
    return total % 10 != 0


def payee_account_number(seller):
    """The vendor's payee account: `iban`, else `account_number`, else None."""
    if not isinstance(seller, dict):
        return None
    return (seller.get("iban") or "").strip() or (
        seller.get("account_number") or ""
    ).strip() or None


def payee_institution_id(seller):
    """The bank identifier: BIC, else US routing number. Both map to UBL
    `FinancialInstitutionBranch/ID`.
    """
    if not isinstance(seller, dict):
        return None
    return (seller.get("bic") or "").strip() or (
        seller.get("routing_number") or ""
    ).strip() or None

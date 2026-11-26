# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Bank-number shapes, checksums, and which field holds the payee's account.

🔴 WHY THIS FILE EXISTS. The module was written for a European business, where
"the vendor's account" means one thing: an IBAN. The US deployment is a US business,
where it means a routing number plus an account number, and the two live in
different fields and obey different rules. Rather than scatter `iban or
account_number` through the generator, the post-processing and the bank-side
guard, the whole question is answered here.

WHAT A MISREAD NUMBER COSTS, which is the reason any of this exists: a number
extracted from a PDF by a model becomes a vendor bank account, and the next bill
pays into it. An IBAN carries check digits, and so does a US ABA routing number.
Both are checked before anything downstream can record them.

⚠️ THE TWO CHECKS ARE NOT SYMMETRICAL IN WHAT THEY PROTECT, and it matters:

* An IBAN's mod-97 covers the WHOLE number, so a single misread digit anywhere
  is caught.
* An ABA checksum covers only the nine-digit ROUTING number — which identifies
  the bank, not the account. A US **account number** has no checksum at all:
  nothing about `1234567890` distinguishes it from `1234567891`. So on a US bill
  the routing number is verified and the account number simply cannot be, and no
  amount of code changes that. That is a limit of the data, and it is written
  down here so nobody later assumes US numbers are as protected as IBANs.
"""

import re

# Country code, two check digits, 11-30 alphanumerics: the shape of an IBAN.
IBAN_SHAPE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")

# A US ABA routing transit number: exactly nine digits.
ABA_SHAPE = re.compile(r"^\d{9}$")


def sanitize(account_number):
    """Strip punctuation and whitespace, uppercase. Mirrors Odoo's
    `sanitize_account_number` without importing the ORM, so this module stays
    usable from a plain unit test."""
    if not account_number:
        return ""
    return re.sub(r"\W+", "", str(account_number)).upper()


def iban_checksum_fails(account_number):
    """True when ``account_number`` looks like an IBAN but fails ISO 13616
    mod-97 — the signature of a misread digit.

    🔴 SHAPE-GATED ON PURPOSE. A number that is not IBAN-shaped is not ours to
    judge, and this returns False for it. That is what keeps the guard inert for
    every US number form — routing numbers, account numbers, hyphenated or
    spaced — rather than rejecting them all. Verified against real routing
    numbers in `tests/test_bank_numbers.py`.
    """
    number = sanitize(account_number)
    if not number or not IBAN_SHAPE.match(number):
        return False
    rearranged = number[4:] + number[:4]
    return int("".join(str(int(char, 36)) for char in rearranged)) % 97 != 1


def aba_checksum_fails(routing_number):
    """True when ``routing_number`` is nine digits and fails the ABA checksum.

    The US counterpart of the IBAN check, and it exists for exactly the same
    reason: a misread routing number sends a payment to the wrong institution.

    3(d1+d4+d7) + 7(d2+d5+d8) + (d3+d6+d9) must be divisible by 10.

    Shape-gated like its IBAN twin: anything that is not nine digits is not a
    routing number, so this declines to judge it.
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
    """The number the vendor is paid INTO, whichever vocabulary it arrived in.

    `iban` first because an IBAN is globally unambiguous; `account_number` is
    the US (and general domestic) field. Returns None when the seller names
    neither — which is normal and simply means the bill states no account.
    """
    if not isinstance(seller, dict):
        return None
    return (seller.get("iban") or "").strip() or (
        seller.get("account_number") or ""
    ).strip() or None


def payee_institution_id(seller):
    """The bank identifier that accompanies the account: BIC/SWIFT abroad, ABA
    routing number in the US.

    ⚠️ These are NOT interchangeable as data — a BIC names an institution
    internationally, a routing number names it within the US clearing system —
    but they occupy the same slot in a UBL document
    (`FinancialInstitutionBranch/ID`), which is the only reason one accessor
    serves both.
    """
    if not isinstance(seller, dict):
        return None
    return (seller.get("bic") or "").strip() or (
        seller.get("routing_number") or ""
    ).strip() or None

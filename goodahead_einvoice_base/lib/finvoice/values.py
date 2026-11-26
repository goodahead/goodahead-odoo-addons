# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Reading single Finvoice values: numbers, dates, identifiers, unit codes.

Pure functions, no lxml, no ERP. Each returns None for an absent value and
raises ``FinvoiceError(finvoice.unreadable)`` for a present value that cannot be
read, so a malformed amount never silently turns into a missing one.
"""

import datetime
import re
from decimal import Decimal, InvalidOperation

from . import units
from .errors import UNREADABLE, FinvoiceError

# guideline §9.4 / data list "amount": a comma is the decimal separator and
# there are no thousands separators. A dot is also accepted (observed: 2.x
# senders writing "12.50"); spaces, including no-break spaces, are dropped
# (observed).
_NUMBER_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")


def parse_decimal(text, field):
    """Finvoice number text -> Decimal, None when absent."""
    if text is None:
        return None
    cleaned = text.replace(" ", "").replace(" ", "").strip()
    if not cleaned:
        return None
    if not _NUMBER_RE.match(cleaned):
        raise FinvoiceError(
            UNREADABLE,
            f"The Finvoice value for {field} ({text.strip()!r}) is not a number.",
            field=field,
        )
    try:
        return Decimal(cleaned.replace(",", "."))
    except InvalidOperation as exc:  # pragma: no cover - the regex already guards
        raise FinvoiceError(
            UNREADABLE,
            f"The Finvoice value for {field} ({text.strip()!r}) is not a number.",
            field=field,
        ) from exc


def parse_date(text, field):
    """Finvoice date -> ``YYYY-MM-DD``, None when absent.

    guideline §19.1: date elements carry Format="CCYYMMDD". An ISO date is also
    accepted (observed in 2.x material).
    """
    if text is None:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        if re.fullmatch(r"\d{8}", cleaned):
            value = datetime.date(int(cleaned[:4]), int(cleaned[4:6]), int(cleaned[6:]))
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
            value = datetime.date.fromisoformat(cleaned)
        else:
            raise ValueError(cleaned)
    except ValueError as exc:
        raise FinvoiceError(
            UNREADABLE,
            f"The Finvoice value for {field} ({cleaned!r}) is not a CCYYMMDD date.",
            field=field,
        ) from exc
    return value.isoformat()


def to_float(value):
    """Decimal -> float for the invoice data; None stays None, -0.0 becomes 0.0."""
    if value is None:
        return None
    result = float(value)
    return 0.0 if result == 0 else result


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------

# Data list, SellerPartyIdentifier: "Business Identity Code = Y-tunnus in
# Finnish form: nnnnnnn-n". The six-digit form is the pre-1998 one written
# without its leading zero (observed).
_BUSINESS_ID_RE = re.compile(r"^\d{6,7}-\d$")


def looks_like_business_id(value):
    """True for the Finnish Business ID shape ``1234567-8`` (observed quirk:
    senders put it in SellerOrganisationTaxCode)."""
    return bool(value) and bool(_BUSINESS_ID_RE.match(value.strip()))


def business_id_checksum_ok(value):
    """Finnish Business ID check digit (weights 7,9,10,5,8,4,2, mod 11)."""
    if not looks_like_business_id(value):
        return False
    digits = value.strip().replace("-", "").zfill(8)
    body, check = digits[:7], int(digits[7])
    remainder = sum(int(d) * w for d, w in zip(body, (7, 9, 10, 5, 8, 4, 2))) % 11
    if remainder == 1:
        return False
    return check == (0 if remainder == 0 else 11 - remainder)


def vat_from_business_id(value):
    """``1234567-8`` -> ``FI12345678``.

    Data list, SellerOrganisationTaxCode: "Vat-number (The Business Identity
    Code with FI as a prefix) FI999999999".
    """
    return "FI" + value.strip().replace("-", "").zfill(8)


_IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")


def normalise_account(value):
    """Account text without spaces, upper-cased."""
    if not value:
        return None
    return re.sub(r"\s+", "", value).upper() or None


def iban_is_valid(value):
    """ISO 13616 mod-97 check on an already normalised account."""
    if not value or not _IBAN_RE.match(value):
        return False
    rearranged = value[4:] + value[:4]
    number = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(number) % 97 == 1


def payment_reference_is_valid(value):
    """True for a Finnish reference number (7-3-1 check digit) or an ISO 11649
    RF reference (mod 97).

    guideline §13: EpiRemittanceInfoIdentifier holds "a reference number
    compliant with the banking standard" — SPY (Finnish) or ISO (RF).
    """
    if not value:
        return False
    ref = re.sub(r"\s+", "", value).upper()
    if ref.startswith("RF"):
        if not re.fullmatch(r"RF\d{2}[A-Z0-9]{1,21}", ref):
            return False
        rearranged = ref[4:] + ref[:4]
        return int("".join(str(int(ch, 36)) for ch in rearranged)) % 97 == 1
    if not re.fullmatch(r"\d{4,20}", ref):
        return False
    body, check = ref[:-1], int(ref[-1])
    weights = (7, 3, 1)
    total = sum(int(d) * weights[i % 3] for i, d in enumerate(reversed(body)))
    return check == (10 - total % 10) % 10


# ---------------------------------------------------------------------------
# Unit codes
# ---------------------------------------------------------------------------

# Data list: quantities carry QuantityUnitCodeUN (UN/ECE Rec 20) and/or a free
# QuantityUnitCode ("eg. kg, m, kwh/h"). Both are read through the one shared
# unit table (``units.normalise_incoming``, send ADR-0013), the same table
# sending writes from: a valid Rec 20 code wins (H87 read as C62), else an
# alias of the free code (kpl, h, kg, ...), else a free code that is itself
# Rec-20 shaped; anything else is None, never a guessed unit.


def unit_code(code_un, code_free):
    """Pick the UN/ECE Rec 20 unit code for a quantity element."""
    return units.normalise_incoming(code_un, code_free)


def unmapped_unit_code(code_un, code_free):
    """The unit code as written when ``unit_code`` cannot read it, else None.

    Kept on the line so the import can say which code nobody mapped instead
    of silently giving the line some unit."""
    if unit_code(code_un, code_free):
        return None
    for code in (code_un, code_free):
        if code and code.strip():
            return code.strip()
    return None

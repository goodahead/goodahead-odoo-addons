# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Global trade item numbers (finvoice content AC-19).

A product barcode goes out as ``EanCode`` (``SchemeID`` ``GTIN_SCHEME``)
only when it is a valid GTIN: 8, 12, 13 or 14 digits with a correct GS1
check digit, and not a restricted-circulation (store-internal) number. It is
sent as stored, never padded; anything else is left out and never blocks.

Pure Python, no ERP import (receiving ADR-0004).
"""

#: ISO 6523 ICD of GS1, written as ``EanCode/@SchemeID``.
GTIN_SCHEME = "0160"

_LENGTHS = (8, 12, 13, 14)
_RESTRICTED_13 = ("02", "04") + tuple(f"2{digit}" for digit in range(10))
_RESTRICTED = {
    8: ("0", "2"),
    12: ("2", "4"),
    13: _RESTRICTED_13,
    14: tuple("0" + prefix for prefix in _RESTRICTED_13),
}


def is_valid_gtin(value):
    """True for a valid, globally unique trade item number."""
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        return False
    if len(value) not in _LENGTHS:
        return False
    if value.startswith(_RESTRICTED[len(value)]):
        return False
    return value[-1] == check_digit(value[:-1])


def check_digit(body):
    """GS1 mod-10 check digit of ``body`` (weights 3, 1, 3, ... from the right)."""
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return str((10 - total % 10) % 10)

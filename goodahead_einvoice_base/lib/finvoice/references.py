# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Order references of an outgoing invoice (finvoice content ADR-0007).

    ours, problem = our_order_number(invoice_origin)
    theirs, problem = customer_order_number(customer_reference, invoice_origin)
    if not seller_reference_allowed(ours, payment_reference): ours = None

Our order number is the invoice's source document as written; the customer's
order number is its customer reference less every comma-separated part that
is one of our order numbers (the ERP echoes our number there when the sale
order has none). A reference over ``MAX_REFERENCE`` is never cut: the value
is ``None`` and the problem ``"too_long"``, which readiness turns into a
warning.

Pure Python, no ERP import (receiving ADR-0004).
"""

from .values import payment_reference_is_valid

#: Finvoice length of OrderIdentifier, SellerReferenceIdentifier and
#: BuyerReferenceIdentifier.
MAX_REFERENCE = 70
TOO_LONG = "too_long"


def our_order_number(source_document):
    """``(value | None, problem | None)``: the source document stripped."""
    value = (source_document or "").strip()
    return _checked(value)


def customer_order_number(customer_reference, source_document):
    """``(value | None, problem | None)``: the customer reference less every
    part equal (trimmed, case-sensitive) to a part of the source document,
    the rest re-joined with ", "."""
    ours = set(_parts(source_document))
    remaining = [part for part in _parts(customer_reference) if part not in ours]
    return _checked(", ".join(remaining))


def seller_reference_allowed(our_number, payment_reference):
    """False when our order number would pass as a payment reference and the
    message carries no valid one."""
    if not our_number or not payment_reference_is_valid(our_number):
        return True
    return payment_reference_is_valid(payment_reference or "")


def _parts(text):
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def _checked(value):
    if not value:
        return None, None
    if len(value) > MAX_REFERENCE:
        return None, TOO_LONG
    return value, None

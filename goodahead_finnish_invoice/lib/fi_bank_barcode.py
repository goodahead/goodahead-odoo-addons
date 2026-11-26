# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Finnish bank bar code (virtuaaliviivakoodi).

The format belongs to the payment, not to the document carrying it, so it
lives here rather than on ``account.move``: an invoice and a proforma print
the same code from the same four values, and a bar code that differs between
the two would be a bar code that pays the wrong way on one of them.

Both versions are exactly 54 digits; they differ in how the reference is
carried.

  version 4 - Finnish reference number
    4 | IBAN without "FI" (16) | euros (6) | cents (2) | zeros (3)
      | reference right-aligned in 20 | due date YYMMDD (6)

  version 5 - RF creditor reference (ISO 11649)
    5 | IBAN without "FI" (16) | euros (6) | cents (2) | RF check digits (2)
      | reference without "RF" and check digits, right-aligned in 21
      | due date YYMMDD (6)

A missing due date is encoded as six zeros.
"""

BARCODE_LENGTH = 54
BARCODE_MAX_AMOUNT = 999999.99
FI_IBAN_LENGTH = 18
NO_DUE_DATE = "000000"


def build_barcode(iban, amount, reference, due_date):
    """Assemble the 54-digit bar code, or False if it doesn't come out right.

    ``iban`` is a sanitized Finnish IBAN, ``amount`` is in euro, ``reference``
    is a Finnish reference number or an RF creditor reference, and ``due_date``
    is a date or None. Callers are expected to have validated the inputs with
    :func:`barcode_input_error`; the length check here is the last guard
    against printing a code no bank would accept.
    """
    account = iban[2:]
    euros, cents = divmod(round(amount * 100), 100)
    amount_digits = "%06d%02d" % (euros, cents)
    due = due_date.strftime("%y%m%d") if due_date else NO_DUE_DATE

    if reference.startswith("RF"):
        body = (
            "5" + account + amount_digits
            + reference[2:4] + reference[4:].rjust(21, "0")
        )
    else:
        body = "4" + account + amount_digits + "000" + reference.rjust(20, "0")

    barcode = body + due
    if len(barcode) != BARCODE_LENGTH:
        return False
    return barcode


def barcode_input_error(iban, amount, reference):
    """Why these values cannot form a bar code, or None when they can.

    Only the payment's own constraints. Whether a given document should carry
    a bar code at all is the document's question, not this module's.
    """
    if not iban:
        return "no recipient bank account"
    # The reason ends up in the log, so it never carries the account number.
    if not iban.startswith("FI") or len(iban) != FI_IBAN_LENGTH:
        return "recipient account is not a Finnish IBAN"

    if amount < 0:
        return "amount is negative"
    if amount > BARCODE_MAX_AMOUNT:
        return "amount exceeds the six digits the bar code allows"

    if not reference:
        return "no payment reference"
    if reference.startswith("RF"):
        if not reference[2:4].isdigit() or not reference[4:].isdigit():
            return "malformed RF reference %s" % reference
        if len(reference[4:]) > 21:
            return "RF reference %s is too long" % reference
    else:
        if not reference.isdigit():
            return "reference %s is not a Finnish reference number" % reference
        if len(reference) > 20:
            return "reference %s is too long" % reference

    return None


# Building a Finnish reference number is deliberately not done here: the
# localisation already owns it. ``l10n_fi_sale`` converts anything written to
# ``sale.order.reference`` through ``compute_payment_reference_finnish``, and
# ``l10n_fi`` offers the same for ``account.move``. A second implementation
# would not merely duplicate that — it would double-encode, because the
# localisation appends its check digit to whatever it is handed, including a
# reference that already carries one.
